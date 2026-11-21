"""阶段二子模块 2：规则绑定（单表 BSP Worker / 跨表 FK / 去重 / 自愈靶向重绑）与 Oracle 资产准备。"""

import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.assets.code_sets.oracle import (
    code_set_compile_bindings,
    compile_code_set_oracle_ddl,
    load_published_code_sets,
    sandbox_code_set_table,
)
from app.engine.binding import SchemaBinding, bind_rules, violates_single_field_rule
from app.engine.compiler import compile_rule_query
from app.engine.oracle import GenericOracleExecutor, compile_ai_schema_mysql_ddl, compile_ai_schema_oracle_ddl
from app.engine.rule_backfill import derive_deterministic_rules as _policy_derive_deterministic_rules
from app.engine.rules import RuleValidationError
from app.engine.workflow.runtime import (
    _call_validated,
    _code_set_context,
    _emit,
    _fanout,
    _inner_width,
    _pending_rules,
    _upsert_rule,
)
from app.engine.workflow.stage_plan_schema import _provider_name
from app.infra.asset_models import ArtifactModel
from app.infra.blob_store import LocalBlobStore
from app.infra.domain import RuleDefinition, calculate_rule_hash, canonical_json
from app.infra.errors import WorkflowError
from app.infra.llm import spec_payload
from app.infra.models import EventModel, RuleModel, RunModel
from app.policy.regops_plan import project


def _summarize_deterministic_rule(rule: RuleDefinition) -> Dict[str, Any]:
    params = rule.params
    if rule.operator == "ENUM" and isinstance(params.get("values"), list) and len(params["values"]) > 12:
        params = {
            "value_count": len(params["values"]),
            "sample_values": params["values"][:8],
        }
    return {
        "rule_key": rule.rule_key,
        "operator": rule.operator,
        "params": params,
    }


def _drop_unsatisfiable_field_compare_eq(
    all_table_rules: List[RuleDefinition],
    bound_rules: List[RuleDefinition],
) -> List[RuleDefinition]:
    """剔除数学上无解的 FIELD_COMPARE(EQ)：若一侧有限 ENUM 全部取值均违反另一侧单字段规则，整列判等必无解。"""
    enum_by_field: Dict[str, List[str]] = {}
    constraints_by_field: Dict[str, List[RuleDefinition]] = {}
    for rule in all_table_rules:
        constraints_by_field.setdefault(rule.field, []).append(rule)
        if rule.operator == "ENUM" and rule.params.get("values"):
            vals = [str(v) for v in rule.params["values"]]
            if rule.field not in enum_by_field or len(vals) < len(enum_by_field[rule.field]):
                enum_by_field[rule.field] = vals
    kept: List[RuleDefinition] = []
    for rule in bound_rules:
        if rule.operator == "FIELD_COMPARE" and (rule.params or {}).get("operator") == "EQ":
            right = (rule.params or {}).get("right_field")
            unsat = False
            for src, dst in ((rule.field, right), (right, rule.field)):
                vals = enum_by_field.get(src)
                if not vals or not dst:
                    continue
                dst_rules = [
                    dr
                    for dr in constraints_by_field.get(dst, [])
                    if dr.operator in {"ENUM", "NOT_IN", "LENGTH", "LENGTH_MAX", "REGEXP"}
                ]
                if dst_rules and all(
                    any(violates_single_field_rule(dr.operator, dr.params or {}, v) is True for dr in dst_rules)
                    for v in vals
                ):
                    unsat = True
                    break
            if unsat:
                continue
        kept.append(rule)
    return kept


def prepare_deterministic_rule_index(deterministic_rules: List[RuleDefinition]) -> Dict[str, Any]:
    """将确定性规则整理为只读索引，供单表 BSP Worker 线程无锁读取。"""
    det_by_field: Dict[tuple, List[RuleDefinition]] = {}
    det_signatures = set()
    det_single_ops = set()
    det_keys = set()
    for rule in deterministic_rules:
        det_by_field.setdefault((rule.table_code, rule.field), []).append(rule)
        det_signatures.add((rule.table_code, rule.field, rule.operator, canonical_json(rule.params)))
        if rule.operator in {"REQUIRED", "LENGTH_MAX"}:
            det_single_ops.add((rule.table_code, rule.field, rule.operator))
        det_keys.add(rule.rule_key)
    return {
        "deterministic_rules": deterministic_rules,
        "det_by_field": det_by_field,
        "det_signatures": det_signatures,
        "det_single_ops": det_single_ops,
        "det_keys": det_keys,
    }


def bind_table_rules_worker(
    spec,
    *,
    llm,
    plans: Optional[Dict[str, Any]],
    field_code_sets: Dict[Any, Any],
    det_index: Dict[str, Any],
    concurrency: int = 4,
    outer_workers: int = 1,
    bus: Any = None,
) -> Tuple[List[RuleDefinition], Dict[str, Any]]:
    """纯单表规则绑定 Worker（零 DB 写操作，可安全并发运行在 Scheduler BSP Send 波次内）。"""
    det_by_field = det_index["det_by_field"]
    det_signatures = det_index["det_signatures"]
    det_single_ops = det_index["det_single_ops"]
    det_keys = det_index["det_keys"]
    deterministic_rules = det_index["deterministic_rules"]

    bound_rules: List[RuleDefinition] = []
    batch_traces: List[Dict[str, Any]] = []
    gaps: List[Dict[str, Any]] = []
    fields = spec.fields

    def _bind_one_field(field):
        field_det = det_by_field.get((spec.table_code, field.name), [])
        payload = {
            **spec_payload(spec),
            "target_field": field.name,
            "fields": [field.model_dump()],
            "field_names": [item.name for item in spec.fields],
            "generation_plan": (plans or {}).get(spec.table_code) or {"mutations_per_rule": 1},
            "deterministic_rules": [_summarize_deterministic_rule(r) for r in field_det],
            "existing_rule_keys": [],
            **_code_set_context(field_code_sets, spec.table_code, field.name),
        }
        rules, trace = _call_validated(
            llm,
            "assign_rules",
            f"【严格范围】本次仅处理目标字段 `{field.name}`（即 target_field），返回的每条规则的 field 必须等于 `{field.name}`，严禁为 field_names 中的其他字段生成规则（field_names 仅用于填写 if_field / right_field）。"
            "deterministic_rules 中的必填、列宽上限、固定值和已发布码表白名单规则已由系统确定性派生并生效，严禁重复输出。"
            "请一次返回该目标字段剩余所有需要语义理解的规则（如 COND_REQUIRED 条件必填、COND_VALUE 条件必空/条件值域约束、"
            "ARITHMETIC_BALANCE 行内算术平衡勾稽、UNIQUE_KEY 单列/联合唯一键查重、REGEXP 精确格式/定长编码、"
            "NUM_RANGE 数值上下限、FIELD_COMPARE 跨字段比较、NOT_IN 禁用取值或额外业务子集 ENUM）；同一语义只给一条最准确规则，不要输出多个变体；没有则返回空数组。"
            "显性原文与推断拓展必须分开。若提供 published_code_set，ENUM 参数只能取其 values 白名单。"
            "COND_REQUIRED / COND_VALUE 的 if_field 必须严格取自输入 field_names 列表，不得使用简称或臆造字段名。"
            "COND_VALUE 用于「当某条件成立时目标字段必须为空（then_operator: MUST_BE_NULL）或只能取特定子集/区间（then_operator: ENUM|NOT_IN|NUM_RANGE，配 then_values 或 min/max）」。"
            "ARITHMETIC_BALANCE 用于金额/余额平衡公式（如期末=期初+收入-支出、总计=分项1+分项2），params: {terms: [{field, coef}], operator: EQ|GE|LE|GT|LT, target_field 或 target_value, tolerance(可选), null_as_zero(可选)}，所有涉及字段必须为 NUMBER。"
            "UNIQUE_KEY 用于表内唯一键/联合主键不重复，params: {key_fields: [字段列表]}，仅当原文明确要求唯一/不可重复时生成。"
            "数值上下限用 NUM_RANGE（params: min/max/min_inclusive/max_inclusive）；"
            "跨字段比较用 FIELD_COMPARE（params: right_field 必须取自 field_names 且同数据类型，operator 取 LT/LE/GT/GE/EQ/NE，表示左字段必须满足的关系，如到期日>=起息日写 GE；"
            "仅限直接按整列值比较的同语义字段，若原文要求与另一字段的第N位/子串/编码含义一致，禁止用 FIELD_COMPARE）。",
            payload,
            lambda args: bind_rules(spec, args.get("rules", [])),
            bus=bus,
        )
        filtered = [
            rule
            for rule in rules
            if rule.field == field.name
            and (rule.table_code, rule.field, rule.operator, canonical_json(rule.params)) not in det_signatures
            and (rule.table_code, rule.field, rule.operator) not in det_single_ops
        ]
        return filtered, {
            "provider": _provider_name(llm, trace),
            "model": trace.get("model", "unknown"),
            "tool_name": "assign_rules",
            "rounds": [trace],
            "arguments": {"rules": [rule.model_dump() for rule in filtered]},
        }

    def bind_field(field):
        try:
            return _bind_one_field(field)
        except WorkflowError as exc:
            gaps.append(
                {
                    "field": field.name,
                    "error": str(exc),
                    "context": getattr(exc, "context", {}) or {},
                }
            )
            return [], {
                "tool_name": "assign_rules",
                "skipped": True,
                "field": field.name,
                "error": str(exc),
            }

    inner = max(1, min(_inner_width(concurrency, outer_workers), len(fields)))
    if inner <= 1 or len(fields) <= 1:
        field_results = [bind_field(field) for field in fields]
    else:
        with ThreadPoolExecutor(max_workers=inner) as field_executor:
            field_results = list(field_executor.map(bind_field, fields))

    for rules, trace in field_results:
        bound_rules.extend(rules)
        batch_traces.append(trace)
    table_det_rules = [r for r in deterministic_rules if r.table_code == spec.table_code]
    bound_rules = _drop_unsatisfiable_field_compare_eq(table_det_rules + bound_rules, bound_rules)
    key_counts = {key: 1 for key in det_keys}
    for rule in bound_rules:
        key_counts[rule.rule_key] = key_counts.get(rule.rule_key, 0) + 1
        if key_counts[rule.rule_key] > 1:
            rule.rule_key = f"{rule.rule_key}_{key_counts[rule.rule_key]}"
            rule.rule_hash = calculate_rule_hash(rule, spec.source_sha256)
    if not bound_rules and not table_det_rules and gaps:
        raise WorkflowError(
            f"{spec.table_code} 的 {len(gaps)} 个字段全部未能绑定规则，首个失败：{gaps[0]['error']}",
            context={"stage": "rule_binder", "table_code": spec.table_code, "field": gaps[0]["field"]},
        )
    first_trace = batch_traces[0] if batch_traces else {}
    return bound_rules, {
        "provider": _provider_name(llm, first_trace),
        "model": (first_trace.get("model") or "unknown") if first_trace else "unknown",
        "tool_name": "assign_rules",
        "batch_count": len(batch_traces),
        "deterministic_count": len(table_det_rules),
        "batches": batch_traces,
        "gaps": gaps,
        "arguments": {"rules": [rule.model_dump() for rule in bound_rules]},
    }


def join_table_rules_barrier(
    session: Session,
    run: RunModel,
    specs,
    llm,
    table_results: Dict[str, Dict[str, Any]],
    *,
    bus: Any = None,
    events: Optional[List[tuple]] = None,
) -> Tuple[int, Dict[str, Any]]:
    """规则波次屏障汇聚：落库单表规则 → 跨表外键装配 → 语义去重 → 发射 RULES_BOUND。"""
    gaps_by_table = {
        spec.table_code: table_results[spec.table_code]["gaps"]
        for spec in specs
        if (table_results.get(spec.table_code) or {}).get("gaps")
    }
    if gaps_by_table:
        _emit(
            events,
            session,
            run,
            "RULE_BIND_GAPS",
            "rule_binder",
            {
                "total": sum(len(items) for items in gaps_by_table.values()),
                "tables": gaps_by_table,
                "note": "这些字段没有绑定到规则（模型调用失败或多次校验不过），需要在规则确认环节人工补充",
            },
        )
    table_counts = []
    for spec in specs:
        entry = table_results.get(spec.table_code) or {}
        raw_rules = entry.get("rules") or []
        rules = [
            item if isinstance(item, RuleDefinition) else RuleDefinition.model_validate(item)
            for item in raw_rules
        ]
        table_counts.append({"table_code": spec.table_code, "rule_count": len(rules)})
        for rule in rules:
            _upsert_rule(session, run, rule, "APPROVED" if rule.source == "DOC_EXPLICIT" else "PENDING")

    _emit(
        events,
        session,
        run,
        "BSP_BARRIER_JOIN",
        "rule_barrier",
        {"stage": "rule_binder", "table_results": table_counts},
    )

    cross_rules = []
    golden_overrides: Dict[str, Any] = {}
    if len(specs) > 1:
        selected = {spec.table_code for spec in specs}
        specs_by_code = {spec.table_code: spec for spec in specs}
        compact_tables = [
            {
                "table_code": spec.table_code,
                "table_name": spec.table_name,
                "section": spec.section,
                "fields": [
                    {
                        "name": f.name,
                        "content": f.content,
                        "data_type": f.data_type,
                        "length": f.length,
                        "check_text": (f.check_text or "")[:240],
                    }
                    for f in spec.fields
                ],
                "terms_excerpt": (spec.terms_text or "")[:1500],
            }
            for spec in specs
        ]
        payload = {
            "tables": compact_tables,
            "table_codes": sorted(selected),
            "table_fields": {spec.table_code: [f.name for f in spec.fields] for spec in specs},
            "instruction": "只装配有明确依据的跨表 FOREIGN_KEY 规则（支持异名关联字段），并给必要的黄金记录引用值覆盖。",
        }

        def cross_validator(args):
            bound = []
            for item in args.get("rules", []):
                target = specs_by_code.get(item.get("table_code"))
                if not target:
                    raise RuleValidationError("Cross rule target table is outside the confirmed set")
                bound.extend(bind_rules(target, [item], selected, specs_by_code=specs_by_code))
            overrides = args.get("golden_overrides", {})
            if any(code not in selected for code in overrides):
                raise RuleValidationError("Golden override references an unconfirmed table")
            return (bound, overrides)

        (cross_rules, golden_overrides), trace = _call_validated(
            llm,
            "assign_rules",
            "装配跨表 FOREIGN_KEY 规则。rule 必须额外带 table_code；references_table 必须来自已确认表，"
            "references_field 必须来自目标表的 table_fields 白名单（允许异名字段引用，如 CUST_ID 引用 ID）。可返回 golden_overrides 对齐引用值。",
            payload,
            cross_validator,
            bus=bus,
        )
        _emit(events, session, run, "AI_TOOL_ASSIGNED", "cross_rule_binder", trace)
        for rule in cross_rules:
            _upsert_rule(session, run, rule, "APPROVED" if rule.source == "DOC_EXPLICIT" else "PENDING")

    _emit(
        events,
        session,
        run,
        "CROSS_RULES_ASSIGNED",
        "cross_rule_binder",
        {"rule_keys": [rule.rule_key for rule in cross_rules], "golden_overrides": golden_overrides},
    )
    _dedupe_approved_rules(session, run, events=events)
    bound = session.query(RuleModel).filter(RuleModel.run_id == run.id).all()
    pending_count = len(_pending_rules(session, run.id))
    _emit(
        events,
        session,
        run,
        "RULES_BOUND",
        "rule_barrier",
        {
            "pending_count": pending_count,
            "origin": "rule_binder",
            "rule_set": [
                {"rule_key": rule.rule_key, "rule_hash": rule.rule_hash, "source": rule.source, "table_code": rule.table_code}
                for rule in sorted(bound, key=lambda item: (item.table_code, item.rule_key))
            ],
        },
    )
    return pending_count, golden_overrides


def _rules_stage(
    session,
    run,
    specs,
    llm,
    plans=None,
    concurrency: int = 4,
    bus=None,
    events: Optional[List[tuple]] = None,
):
    _, field_code_sets = load_published_code_sets(session, specs)
    deterministic_rules = _policy_derive_deterministic_rules(
        session,
        run,
        specs,
        _upsert_rule,
        _emit,
        field_code_sets=field_code_sets,
        events=events,
    )
    det_index = prepare_deterministic_rule_index(deterministic_rules)
    outer_workers = max(1, min(concurrency, len(specs)))

    def worker(spec):
        return bind_table_rules_worker(
            spec,
            llm=llm,
            plans=plans,
            field_code_sets=field_code_sets,
            det_index=det_index,
            concurrency=concurrency,
            outer_workers=outer_workers,
            bus=bus,
        )

    results = _fanout(session, run, "rule_binder", specs, worker, max_workers=concurrency, events=events, bus=bus)
    table_results = {
        spec.table_code: {"rules": rules, "gaps": trace.get("gaps") or [], "trace": trace}
        for spec, (rules, trace) in zip(specs, results)
    }
    return join_table_rules_barrier(session, run, specs, llm, table_results, bus=bus, events=events)


def _ensure_oracle_assets(
    session,
    run,
    specs,
    schema_payload,
    rules,
    settings,
    events: Optional[List[tuple]] = None,
    attempt: Optional[int] = None,
):
    if attempt is None:
        attempt = project(session, run).attempt + 1
    existing = (
        session.query(EventModel)
        .filter(EventModel.run_id == run.id, EventModel.kind == "ORACLE_ASSETS_PREPARED")
        .order_by(EventModel.seq.desc())
        .first()
    )
    if existing and existing.payload_json.get("attempt") == attempt:
        return existing.payload_json["assets"]

    typed_schemas = {
        code: [SchemaBinding.model_validate(item) for item in items]
        for code, items in schema_payload.items()
    }
    code_sets, field_code_sets = load_published_code_sets(session, specs)
    code_set_bindings = code_set_compile_bindings(
        field_code_sets,
        {item["code"]: sandbox_code_set_table(run.id, attempt, item["code"]) for item in code_sets},
    )
    assets = GenericOracleExecutor(settings.oracle_sandbox_url).prepare_assets(
        run.id, attempt, specs, typed_schemas, code_sets=code_sets, rules=rules, code_set_bindings=code_set_bindings
    )
    blobs = LocalBlobStore(settings.blob_dir)
    ddl_uri = blobs.put(
        "\n\n".join(compile_ai_schema_mysql_ddl(spec.table_code, typed_schemas[spec.table_code]) for spec in specs).encode()
    )
    oracle_ddl_parts = [
        compile_code_set_oracle_ddl(assets["code_set_tables"][code_set["code"]]) for code_set in code_sets
    ] + [
        compile_ai_schema_oracle_ddl(assets["target_tables"][spec.table_code], typed_schemas[spec.table_code])
        for spec in specs
    ]
    oracle_ddl_uri = blobs.put("\n\n".join(oracle_ddl_parts).encode())
    queries = []
    for rule in rules:
        sql, params = compile_rule_query(rule, "oracle", assets["physical_tables"], code_set_bindings)
        queries.append({"rule_key": rule.rule_key, "table_code": rule.table_code, "sql": sql, "params": params})
    sql_uri = blobs.put(canonical_json({"dialect": "oracle", "queries": queries}).encode())
    result = {
        "ddl_blob": ddl_uri,
        "oracle_ddl_blob": oracle_ddl_uri,
        "validation_sql_blob": sql_uri,
        "oracle_tables": assets["physical_tables"],
        "oracle_target_tables": assets["target_tables"],
        "oracle_code_set_tables": assets["code_set_tables"],
        "oracle_precheck": assets["empty_run"],
    }
    for artifact_type, uri in [("MYSQL_DDL", ddl_uri), ("ORACLE_DDL", oracle_ddl_uri), ("VALIDATION_SQL", sql_uri)]:
        session.add(
            ArtifactModel(
                id=str(uuid.uuid4()),
                run_id=run.id,
                artifact_type=artifact_type,
                blob_uri=uri,
                sha256=uri.removeprefix("blob://"),
                version=attempt,
            )
        )
    _emit(
        events,
        session,
        run,
        "ORACLE_ASSETS_PREPARED",
        "oracle_asset_builder",
        {"attempt": attempt, "assets": assets, "result": result},
    )
    session.flush()
    return assets


def _dedupe_approved_rules(session: Session, run: RunModel, events: Optional[List[tuple]] = None) -> int:
    """对同批次的 APPROVED 与 PENDING 规则统一做语义去重（APPROVED 优先保留，避免重复 PENDING 干扰人工确权）。"""
    rows = (
        session.query(RuleModel)
        .filter(RuleModel.run_id == run.id, RuleModel.status.in_(["APPROVED", "PENDING"]))
        .order_by(
            RuleModel.status.asc(),
            RuleModel.source.desc(),
            RuleModel.created_at,
            RuleModel.rule_key,
        )
        .all()
    )
    seen = set()
    superseded = []
    for row in rows:
        definition = row.definition_json
        signature = (
            row.table_code,
            definition.get("field"),
            definition.get("operator"),
            canonical_json(definition.get("params", {})),
        )
        if signature in seen:
            row.status = "SUPERSEDED"
            superseded.append({"rule_key": row.rule_key, "rule_hash": row.rule_hash})
        else:
            seen.add(signature)
    if superseded:
        _emit(events, session, run, "RULE_DUPLICATES_SUPERSEDED", "rule_barrier", {"rules": superseded})
        session.flush()
    return len(superseded)


def _rebind_failed_rules(
    session,
    run,
    specs,
    llm,
    report,
    bus=None,
    events: Optional[List[tuple]] = None,
) -> int:
    changed_count = _dedupe_approved_rules(session, run, events=events)
    failed_keys = sorted(
        {rule_key for case in report.get("cases", []) for rule_key in case.get("missing_rules", [])}
    )
    if not failed_keys:
        return 0
    rows = (
        session.query(RuleModel)
        .filter(RuleModel.run_id == run.id, RuleModel.rule_key.in_(failed_keys), RuleModel.status == "APPROVED")
        .all()
    )
    revised_count = 0
    for row in rows:
        spec = next(item for item in specs if item.table_code == row.table_code)
        current = RuleDefinition.model_validate(row.definition_json)
        failed_cases = [case for case in report.get("cases", []) if row.rule_key in case.get("missing_rules", [])]
        payload = {
            **spec_payload(spec),
            "fields": [next(field for field in spec.fields if field.name == current.field).model_dump()],
            "current_rule": current.model_dump(),
            "failed_cases": failed_cases,
        }

        def validate_revision(args):
            revised = bind_rules(spec, args.get("rules", []))
            if len(revised) != 1:
                raise RuleValidationError("A failed rule must be revised into exactly one rule")
            if revised[0].rule_hash == current.rule_hash:
                raise RuleValidationError("The revised rule is identical to the failed version")
            if revised[0].operator != current.operator:
                raise RuleValidationError(
                    f"修正不能更换 operator（当前 {current.operator}）。"
                    f"若原文还要求 {revised[0].operator}，那是另一条规则，"
                    "请保持当前 operator 只调整 params。"
                )
            revised[0].rule_key = current.rule_key
            revised[0].rule_hash = calculate_rule_hash(revised[0], spec.source_sha256)
            return revised[0]

        try:
            revised, trace = _call_validated(
                llm,
                "assign_rules",
                "根据失败记录修正当前规则赋值。长度一律用 LENGTH_MAX（列宽是上限）；"
                "要卡「必须正好 N 位」请用 REGEXP 写死格式；数值上下限用 NUM_RANGE（min/max/min_inclusive/max_inclusive）；"
                "跨字段比较用 FIELD_COMPARE（right_field 取自输入字段列表，operator:LT|LE|GT|GE|EQ|NE）；"
                "只返回修正后的一条规则。",
                payload,
                validate_revision,
                bus=bus,
            )
        except WorkflowError as exc:
            _emit(
                events,
                session,
                run,
                "RULE_REBIND_GAP",
                "rule_binder",
                {"table_code": spec.table_code, "rule_key": current.rule_key, "error": str(exc)},
            )
            continue
        _emit(
            events,
            session,
            run,
            "AI_TOOL_ASSIGNED",
            "rule_binder",
            {"table_code": spec.table_code, "rebind_for": current.rule_key, **trace},
        )
        row.status = "SUPERSEDED"
        _upsert_rule(session, run, revised, "APPROVED" if revised.source == "DOC_EXPLICIT" else "PENDING")
        revised_count += 1
    return changed_count + revised_count
