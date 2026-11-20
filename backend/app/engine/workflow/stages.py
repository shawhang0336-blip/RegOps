"""阶段二各核心执行阶段实现（Workflow Stages）。

按流水线顺序包含：
1. `_plan_stage`：基于预算为每张表生成必做/拓展规则数与造数规模规划；
2. `_schema_stage`：字段物理存储类型与长度绑定；
3. `_rules_stage` / `_dedupe_approved_rules` / `_rebind_failed_rules`：单表/跨表规则装配与自愈靶向重绑；
4. `_ensure_oracle_assets`：Oracle 沙箱表与码表预建、MySQL/Oracle DDL 及校验 SQL 产物固化；
5. `_data_stage` + 三类确定性规则回填包装：黄金合规样本生成器绑定；
6. `_audit_stage`：黄金基线与单点变异审计用例绑定。
"""

import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.assets.code_sets.oracle import (
    code_set_compile_bindings,
    compile_code_set_oracle_ddl,
    load_published_code_sets,
    sandbox_code_set_table,
)
from app.engine.binding import (
    GeneratorBinding,
    SchemaBinding,
    _validate_generator_item,
    bind_audit_cases,
    bind_generators,
    bind_rules,
    bind_schema,
    compile_schema_bindings,
    find_golden_rule_violations,
    materialize_golden_record,
    normalize_case_id,
)
from app.engine.mutations import (
    build_deterministic_mutations,
    build_golden_audit_case,
    build_regexp_fallback_mutation,
)
from app.engine.compiler import compile_rule_query
from app.infra.domain import RuleDefinition, calculate_rule_hash, canonical_json
from app.infra.llm import spec_payload
from app.engine.oracle import GenericOracleExecutor, compile_ai_schema_mysql_ddl, compile_ai_schema_oracle_ddl
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
from app.infra.asset_models import (
    ArtifactModel,
    AuditCaseDefinitionModel,
    GenerationPlanModel,
    GeneratorDefinitionModel,
    SchemaDefinitionModel,
)
from app.infra.errors import WorkflowError
from app.infra.models import EventModel, RuleModel, RunModel
from app.kernel.blob_store import LocalBlobStore
from app.policy.regops_plan import project
from app.engine.rule_backfill import (
    _backfill_code_set_enum_rules as _policy_backfill_code_set_enum_rules,
    _backfill_fixed_value_rules as _policy_backfill_fixed_value_rules,
    _backfill_length_rules as _policy_backfill_length_rules,
    _backfill_required_rules as _policy_backfill_required_rules,
    _code_set_enum_backfill_candidates,
    _declaration_quote,
    _fixed_value_candidates,
    _length_backfill_candidates,
    _required_backfill_candidates,
    derive_deterministic_rules as _policy_derive_deterministic_rules,
)


def _provider_name(llm, trace: Optional[Dict[str, Any]] = None) -> str:
    if isinstance(trace, dict) and trace.get("provider"):
        return str(trace["provider"])
    settings = getattr(llm, "settings", None)
    if settings is not None and getattr(settings, "llm_provider", None):
        return str(settings.llm_provider)
    return "unknown"


def _validate_plan(args, budget):
    plan = {
        "mandatory_rule_target": int(args["mandatory_rule_target"]),
        "expanded_rule_target": int(args["expanded_rule_target"]),
        "golden_count": int(args["golden_count"]),
        "mutations_per_rule": int(args["mutations_per_rule"]),
        "rationale": args["rationale"],
    }
    if plan["mandatory_rule_target"] < 0 or plan["expanded_rule_target"] < 0:
        raise RuleValidationError("Rule targets cannot be negative")
    if plan["mandatory_rule_target"] + plan["expanded_rule_target"] > budget["max_rules_per_table"]:
        raise RuleValidationError("Rule target exceeds the configured budget")
    if plan["golden_count"] < 1 or plan["mutations_per_rule"] < 1:
        raise RuleValidationError("Golden count and mutations must be positive")
    estimated = plan["golden_count"] + plan["mutations_per_rule"] * (plan["mandatory_rule_target"] + plan["expanded_rule_target"])
    if estimated > budget["max_cases_per_table"]:
        raise RuleValidationError("Estimated audit cases exceed the configured budget")
    return plan


def _plan_stage(session, run, specs, llm, budget, concurrency: int = 4, bus=None, events: Optional[List[tuple]] = None):
    def worker(spec):
        return _call_validated(
            llm,
            "assign_generation_plan",
            "根据表复杂度自行决定必做规则数、拓展规则数、黄金数据量和每规则变异数，并说明理由；不得超出输入预算。",
            {**spec_payload(spec), "budget": budget},
            lambda args: _validate_plan(args, budget),
            bus=bus,
        )

    results = _fanout(session, run, "generation_planner", specs, worker, max_workers=concurrency, events=events, bus=bus)
    plans = {spec.table_code: result for spec, (result, _) in zip(specs, results)}
    _emit(
        events,
        session,
        run,
        "GENERATION_PLANNED",
        "plan_barrier",
        {"tables": plans, "budget": budget},
    )
    for spec, (_, trace) in zip(specs, results):
        session.add(
            GenerationPlanModel(
                id=str(uuid.uuid4()),
                run_id=run.id,
                table_code=spec.table_code,
                plan_json=plans[spec.table_code],
                trace_json=trace,
            )
        )
    session.flush()
    _emit(
        events,
        session,
        run,
        "BSP_BARRIER_JOIN",
        "plan_barrier",
        {"stage": "generation_planner", "table_codes": list(plans)},
    )
    return plans


def _schema_stage(session, run, specs, llm, concurrency: int = 4, bus=None, events: Optional[List[tuple]] = None, blob_store: Optional[LocalBlobStore] = None):
    """确定性 Schema 映射阶段：根据已确认字段元数据直接编译物理存储类型，不消耗 AI 调用。"""
    schemas = {spec.table_code: compile_schema_bindings(spec) for spec in specs}
    for spec in specs:
        session.add(
            SchemaDefinitionModel(
                id=str(uuid.uuid4()),
                run_id=run.id,
                table_code=spec.table_code,
                version=1,
                definition_json={"fields": [item.model_dump() for item in schemas[spec.table_code]]},
            )
        )
    session.flush()
    tables_payload = {code: [item.model_dump() for item in items] for code, items in schemas.items()}
    event_payload: Dict[str, Any] = {
        "strategy": "DETERMINISTIC_SCHEMA_COMPILER",
        "tables": tables_payload,
    }
    if blob_store is not None:
        event_payload["tables_blob"] = blob_store.put(canonical_json(tables_payload).encode("utf-8"))
    _emit(
        events,
        session,
        run,
        "SCHEMA_ASSIGNED",
        "schema_barrier",
        event_payload,
    )
    _emit(
        events,
        session,
        run,
        "BSP_BARRIER_JOIN",
        "schema_barrier",
        {"stage": "schema_mapper", "table_codes": list(schemas)},
    )
    return tables_payload


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


def _rules_stage(session, run, specs, llm, plans=None, concurrency: int = 4, bus=None, events: Optional[List[tuple]] = None):
    _, field_code_sets = load_published_code_sets(session, specs)
    # 确定性派生先行：在调用 AI 之前先机械产出必填 / 列宽上限 / 固定值 / 已发布码表 ENUM，
    # 让 AI 专注需要语义推理的复杂规则，且人工在第二步就能看到完整规则全集。
    deterministic_rules = _policy_derive_deterministic_rules(
        session,
        run,
        specs,
        _upsert_rule,
        _emit,
        field_code_sets=field_code_sets,
        events=events,
    )
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

    outer_workers = max(1, min(concurrency, len(specs)))

    def worker(spec):
        bound_rules = []
        batch_traces = []
        gaps: List[Dict[str, Any]] = []
        fields = spec.fields

        def bind_field(field):
            try:
                return _bind_one_field(field)
            except WorkflowError as exc:
                # 单个字段绑不出规则不该毁掉整张表：记账、跳过，让人在规则确认环节补
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
                "请一次返回该目标字段剩余所有需要语义理解的规则（如 COND_REQUIRED 条件必填、REGEXP 精确格式/定长编码、"
                "NUM_RANGE 数值上下限、FIELD_COMPARE 跨字段比较、NOT_IN 禁用取值或额外业务子集 ENUM）；同一语义只给一条最准确规则，不要输出多个变体；没有则返回空数组。"
                "显性原文与推断拓展必须分开。若提供 published_code_set，ENUM 参数只能取其 values 白名单。"
                "COND_REQUIRED 的 if_field 必须严格取自输入 field_names 列表，不得使用简称或臆造字段名。"
                "数值上下限用 NUM_RANGE（params: min/max/min_inclusive/max_inclusive）；"
                "跨字段比较用 FIELD_COMPARE（params: right_field 必须取自 field_names，operator 取 LT/LE/GT/GE/EQ/NE，表示左字段必须满足的关系，如到期日>=起息日写 GE）。",
                payload,
                lambda args: bind_rules(spec, args.get("rules", [])),
                bus=bus,
            )
            # 严格过滤越界字段规则（防止模型顺手为同表其他字段重复生成规则）及与确定性派生重复的规则
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

        with ThreadPoolExecutor(max_workers=max(1, min(_inner_width(concurrency, outer_workers), len(fields)))) as field_executor:
            field_results = list(field_executor.map(bind_field, fields))
        for rules, trace in field_results:
            bound_rules.extend(rules)
            batch_traces.append(trace)
        key_counts = {key: 1 for key in det_keys}
        for rule in bound_rules:
            key_counts[rule.rule_key] = key_counts.get(rule.rule_key, 0) + 1
            if key_counts[rule.rule_key] > 1:
                rule.rule_key = f"{rule.rule_key}_{key_counts[rule.rule_key]}"
                rule.rule_hash = calculate_rule_hash(rule, spec.source_sha256)
        table_det_rules = [r for r in deterministic_rules if r.table_code == spec.table_code]
        if not bound_rules and not table_det_rules and gaps:
            raise WorkflowError(
                f"{spec.table_code} 的 {len(gaps)} 个字段全部未能绑定规则，"
                f"首个失败：{gaps[0]['error']}",
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

    results = _fanout(session, run, "rule_binder", specs, worker, max_workers=concurrency, events=events, bus=bus)
    gaps_by_table = {
        spec.table_code: trace["gaps"]
        for spec, (_, trace) in zip(specs, results)
        if trace.get("gaps")
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
    for spec, (rules, _) in zip(specs, results):
        for rule in rules:
            _upsert_rule(session, run, rule, "APPROVED" if rule.source == "DOC_EXPLICIT" else "PENDING")
    _emit(
        events,
        session,
        run,
        "BSP_BARRIER_JOIN",
        "rule_barrier",
        {
            "stage": "rule_binder",
            "table_results": [
                {"table_code": spec.table_code, "rule_count": len(rules)}
                for spec, (rules, _) in zip(specs, results)
            ],
        },
    )
    cross_rules = []
    golden_overrides = {}
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


def _ensure_oracle_assets(session, run, specs, schema_payload, rules, settings, events: Optional[List[tuple]] = None, attempt: Optional[int] = None):
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

    from app.engine.binding import SchemaBinding

    typed_schemas = {
        code: [SchemaBinding.model_validate(item) for item in items]
        for code, items in schema_payload.items()
    }
    code_sets, field_code_sets = load_published_code_sets(session, specs)
    # 码表子查询用沙箱表名：预检建表和规则空跑在同一批沙箱对象上执行
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
        session.add(ArtifactModel(
            id=str(uuid.uuid4()), run_id=run.id, artifact_type=artifact_type, blob_uri=uri,
            sha256=uri.removeprefix("blob://"), version=attempt,
        ))
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


def _next_asset_version(session, model, run_id, table_code) -> int:
    current = session.query(func.max(model.version)).filter(model.run_id == run_id, model.table_code == table_code).scalar()
    return int(current or 0) + 1


_GENERATOR_BATCH_SIZE = 12
_SEMANTIC_GENERATOR_OPERATORS = {"REGEXP", "NUM_RANGE", "FIELD_COMPARE", "COND_REQUIRED", "FOREIGN_KEY"}


def _coupled_field_names(
    spec,
    table_rules: List[RuleDefinition],
    heal_report: Optional[Dict[str, Any]] = None,
) -> set:
    """识别需要 AI 联合语义赋值的字段（正则、数值区间、跨字段比较、条件必填、外键或误报自愈字段）。"""
    coupled = set()
    rule_by_key = {rule.rule_key: rule for rule in table_rules}
    for rule in table_rules:
        if rule.operator in _SEMANTIC_GENERATOR_OPERATORS:
            coupled.add(rule.field)
            if rule.operator == "FIELD_COMPARE" and rule.params.get("right_field"):
                coupled.add(rule.params["right_field"])
            if rule.operator == "COND_REQUIRED" and rule.params.get("if_field"):
                coupled.add(rule.params["if_field"])
    if heal_report:
        for case in heal_report.get("cases", []):
            if case.get("table_code") != spec.table_code or case.get("case_kind") != "GOLDEN":
                continue
            for rule_key in case.get("unexpected_rules") or []:
                rule = rule_by_key.get(rule_key)
                if rule:
                    coupled.add(rule.field)
                    if rule.params.get("right_field"):
                        coupled.add(rule.params["right_field"])
                    if rule.params.get("if_field"):
                        coupled.add(rule.params["if_field"])
    return coupled


def _deterministic_generator_item(
    field,
    table_code: str,
    table_rules: List[RuleDefinition],
    field_code_sets: Dict[Any, Any],
    required_ref_values: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """对无复杂语义约束的字段直接确定性构造合规生成器，减少 AI 杂活并保证码表对齐。"""
    if field.name in required_ref_values:
        return {"field": field.name, "generator": "FIXED", "params": {"value": required_ref_values[field.name]}}

    field_rules = [rule for rule in table_rules if rule.field == field.name]
    forbidden = {
        str(val)
        for rule in field_rules
        if rule.operator == "NOT_IN"
        for val in (rule.params.get("values") or [])
    }
    length_limits = [int(field.length)] if field.length else []
    for rule in field_rules:
        if rule.operator in {"LENGTH_MAX", "LENGTH"} and rule.params.get("length"):
            length_limits.append(int(rule.params["length"]))
    max_len = min(length_limits) if length_limits else 64

    code_set = field_code_sets.get((table_code, field.name))
    if code_set and code_set.get("values"):
        candidates = [
            str(item["value"])
            for item in code_set["values"]
            if item.get("value") is not None and str(item["value"]) not in forbidden
        ]
        if not candidates:
            return None
        return {"field": field.name, "generator": "ENUM", "params": {"values": candidates[:10]}}

    enum_rules = [rule for rule in field_rules if rule.operator == "ENUM" and rule.params.get("values")]
    if enum_rules:
        allowed_sets = [
            [str(v) for v in rule.params["values"] if str(v) not in forbidden]
            for rule in enum_rules
        ]
        common = allowed_sets[0]
        for other in allowed_sets[1:]:
            other_set = set(other)
            common = [v for v in common if v in other_set]
        if not common:
            return None
        if len(common) == 1:
            return {"field": field.name, "generator": "FIXED", "params": {"value": common[0]}}
        return {"field": field.name, "generator": "ENUM", "params": {"values": common[:10]}}

    if (field.data_type or "").upper() == "NUMBER":
        for cand in (1, 10, 2, 100):
            if str(cand) not in forbidden:
                return {"field": field.name, "generator": "FIXED", "params": {"value": cand}}
        return None

    exact_lengths = [int(rule.params["length"]) for rule in field_rules if rule.operator == "LENGTH" and rule.params.get("length")]
    if exact_lengths:
        exact = exact_lengths[0]
        candidate = ("A" * exact) if exact <= 64 else ("A" * exact)
        if candidate not in forbidden:
            return {"field": field.name, "generator": "FIXED", "params": {"value": candidate}}
        return None

    for base in ("SAMPLE_OK", "OK", "A", "B", "1", "V"):
        cand = base[: max(1, max_len)]
        if cand and cand not in forbidden:
            return {"field": field.name, "generator": "FIXED", "params": {"value": cand}}
    return None


def _data_stage(
    session,
    run,
    specs,
    llm,
    active_rules,
    golden_overrides,
    plans=None,
    concurrency: int = 4,
    bus=None,
    events: Optional[List[tuple]] = None,
    blob_store: Optional[LocalBlobStore] = None,
    heal_report: Optional[Dict[str, Any]] = None,
):
    _, field_code_sets = load_published_code_sets(session, specs)

    def worker(spec):
        table_rules = [rule for rule in active_rules if rule.table_code == spec.table_code]
        rule_views = [rule.model_dump() for rule in table_rules]
        required_refs = golden_overrides.get(spec.table_code, {})
        golden_count = int(((plans or {}).get(spec.table_code) or {}).get("golden_count", 1))
        coupled = _coupled_field_names(spec, table_rules, heal_report=heal_report)

        by_field_name: Dict[str, Dict[str, Any]] = {}
        ai_fields = []
        for field in spec.fields:
            if field.name in coupled:
                ai_fields.append(field)
                continue
            det_item = _deterministic_generator_item(field, spec.table_code, table_rules, field_code_sets, required_refs)
            if det_item is not None:
                try:
                    binding = GeneratorBinding.model_validate(det_item)
                    _validate_generator_item(field, binding)
                    det_item["params"] = binding.params
                    by_field_name[field.name] = det_item
                    continue
                except Exception:
                    pass
            ai_fields.append(field)

        # 若确定性字段本身触犯任何规则（极端组合冲突），自动升级交给 AI 联合求解
        if by_field_name and not ai_fields:
            prelim_bindings = [GeneratorBinding.model_validate(by_field_name[f.name]) for f in spec.fields]
            prelim_record = materialize_golden_record(spec, prelim_bindings, 0)
            prelim_violations = find_golden_rule_violations(
                spec, table_rules, prelim_record, prelim_bindings, golden_count, golden_overrides=required_refs
            )
            if prelim_violations:
                conflict_fields = {v["field"] for v in prelim_violations}
                for field in spec.fields:
                    if field.name in conflict_fields:
                        by_field_name.pop(field.name, None)
                        ai_fields.append(field)

        batch_traces: List[Dict[str, Any]] = []
        if by_field_name:
            batch_traces.append(
                {
                    "strategy": "DETERMINISTIC_GENERATORS",
                    "field_count": len(by_field_name),
                    "fields": sorted(by_field_name),
                }
            )

        heal_feedback_cases = []
        if heal_report:
            heal_feedback_cases = [
                case
                for case in heal_report.get("cases", [])
                if case.get("table_code") == spec.table_code and case.get("case_kind") == "GOLDEN" and case.get("unexpected_rules")
            ]

        chunks = [ai_fields[i : i + _GENERATOR_BATCH_SIZE] for i in range(0, len(ai_fields), _GENERATOR_BATCH_SIZE)]
        for chunk in chunks:
            chunk_names = [f.name for f in chunk]
            chunk_name_set = set(chunk_names)
            chunk_by_name = {f.name: f for f in chunk}
            det_golden_preview = materialize_golden_record(
                spec,
                [GeneratorBinding.model_validate(item) for item in by_field_name.values()],
                0,
            ) if by_field_name else {}
            chunk_code_sets = {
                f.name: _code_set_context(field_code_sets, spec.table_code, f.name)["published_code_set"]
                for f in chunk
                if (spec.table_code, f.name) in field_code_sets
            }
            payload: Dict[str, Any] = {
                **spec_payload(spec),
                "fields": [f.model_dump() for f in chunk],
                "deterministic_golden_values": det_golden_preview,
                "approved_rules": rule_views,
                "required_reference_values": required_refs,
                "generation_plan": plans.get(spec.table_code) if plans else None,
            }
            if chunk_code_sets:
                payload["published_code_sets"] = chunk_code_sets
                if len(chunk) == 1 and chunk[0].name in chunk_code_sets:
                    payload["published_code_set"] = chunk_code_sets[chunk[0].name]
            if heal_feedback_cases:
                payload["oracle_false_positive_feedback"] = heal_feedback_cases

            def validate_batch(args, _chunk_names=chunk_names, _chunk_name_set=chunk_name_set, _chunk_by_name=chunk_by_name):
                items = args.get("generators", [])
                returned_fields = [item.get("field") for item in items if isinstance(item, dict)]
                if set(returned_fields) != _chunk_name_set or len(items) != len(_chunk_names):
                    raise RuleValidationError(
                        f"Generator response must cover requested fields {_chunk_names} exactly once, got {returned_fields}"
                    )
                validated_items: Dict[str, Dict[str, Any]] = {}
                for item in items:
                    binding = GeneratorBinding.model_validate(item)
                    _validate_generator_item(_chunk_by_name[binding.field], binding)
                    item["params"] = binding.params
                    validated_items[binding.field] = item

                # Python 侧黄金样本自洽预检门禁：在进 Oracle 前拦截与已批准规则冲突的生成器赋值
                merged_map = {**by_field_name, **validated_items}
                merged_bindings = [GeneratorBinding.model_validate(v) for v in merged_map.values()]
                covered_fields = set(merged_map)
                checkable_rules = []
                for rule in table_rules:
                    needed = {rule.field}
                    if rule.operator == "FIELD_COMPARE" and rule.params.get("right_field"):
                        needed.add(rule.params["right_field"])
                    if rule.operator == "COND_REQUIRED" and rule.params.get("if_field"):
                        needed.add(rule.params["if_field"])
                    if needed.issubset(covered_fields):
                        checkable_rules.append(rule)
                sample_rec = materialize_golden_record(spec, merged_bindings, 0)
                violations = find_golden_rule_violations(
                    spec, checkable_rules, sample_rec, merged_bindings, golden_count, golden_overrides=required_refs
                )
                if violations:
                    summary = "；".join(v["message"] for v in violations[:5])
                    raise RuleValidationError(
                        f"黄金样本自洽门禁拦截（测试数据违反已批准规则，请修正生成器参数）：{summary}"
                    )
                return validated_items

            instruction = (
                "为输入的全部关联/约束字段联合赋予黄金合规生成器，确保所有字段同时满足 approved_rules 和 required_reference_values，"
                "并且跨字段比较（FIELD_COMPARE）与条件必填（COND_REQUIRED）彼此自洽、零违规。"
                "FIXED 使用 params.value；ENUM 使用 params.values；SEQUENCE 使用 params.start/step。"
                "若提供 published_code_sets/published_code_set，对应字段的 ENUM values 只能取其白名单子集。"
            )
            if heal_feedback_cases:
                instruction += "注意：上轮 Oracle 审计发现黄金样本触发了误报（见 oracle_false_positive_feedback），请重点修正导致误报的字段取值。"

            validated_map, trace = _call_validated(
                llm,
                "assign_generators",
                instruction,
                payload,
                validate_batch,
                bus=bus,
            )
            by_field_name.update(validated_map)
            batch_traces.append(trace)

        generator_items = [by_field_name[field.name] for field in spec.fields]
        generators = bind_generators(spec, generator_items)
        ai_traces = [tr for tr in batch_traces if tr.get("strategy") != "DETERMINISTIC_GENERATORS"]
        first_ai = ai_traces[0] if ai_traces else None
        return generators, {
            "provider": _provider_name(llm, first_ai) if first_ai else "DETERMINISTIC",
            "model": first_ai.get("model", "unknown") if first_ai else "rule-engine",
            "tool_name": "assign_generators",
            "batch_count": len(batch_traces),
            "deterministic_field_count": len(spec.fields) - len(ai_fields),
            "ai_field_count": len(ai_fields),
            "batches": batch_traces,
            "arguments": {"generators": generator_items},
        }

    results = _fanout(session, run, "data_generator_binder", specs, worker, max_workers=concurrency, events=events, bus=bus)
    generators = {spec.table_code: result for spec, (result, _) in zip(specs, results)}
    golden_records = {
        spec.table_code: materialize_golden_record(spec, generators[spec.table_code]) for spec in specs
    }
    for table_code, overrides in golden_overrides.items():
        if table_code in golden_records:
            golden_records[table_code].update(overrides)
    for spec in specs:
        session.add(
            GeneratorDefinitionModel(
                id=str(uuid.uuid4()),
                run_id=run.id,
                table_code=spec.table_code,
                version=_next_asset_version(session, GeneratorDefinitionModel, run.id, spec.table_code),
                definition_json={
                    "generators": [item.model_dump() for item in generators[spec.table_code]],
                    "golden_record": golden_records[spec.table_code],
                    "generation_plan": (plans or {}).get(spec.table_code),
                },
            )
        )
    session.flush()
    generator_payload = {
        spec.table_code: {
            "generators": [item.model_dump() for item in generators[spec.table_code]],
            "golden_record": golden_records[spec.table_code],
        }
        for spec in specs
    }
    event_payload: Dict[str, Any] = {"tables": generator_payload}
    if blob_store is not None:
        event_payload["tables_blob"] = blob_store.put(canonical_json(generator_payload).encode("utf-8"))
    _emit(
        events,
        session,
        run,
        "GENERATORS_ASSIGNED",
        "data_barrier",
        event_payload,
    )
    _emit(
        events,
        session,
        run,
        "BSP_BARRIER_JOIN",
        "data_barrier",
        {"stage": "data_generator_binder", "table_codes": list(generators)},
    )
    return golden_records, generator_payload


def _backfill_required_rules(session, run, specs, rules, events: Optional[List[tuple]] = None) -> List[RuleDefinition]:
    return _policy_backfill_required_rules(session, run, specs, rules, _upsert_rule, _emit, events=events)


def _backfill_length_rules(session, run, specs, rules, events: Optional[List[tuple]] = None) -> List[RuleDefinition]:
    return _policy_backfill_length_rules(session, run, specs, rules, _upsert_rule, _emit, events=events)


def _backfill_fixed_value_rules(session, run, specs, rules, events: Optional[List[tuple]] = None) -> List[RuleDefinition]:
    return _policy_backfill_fixed_value_rules(session, run, specs, rules, _upsert_rule, _emit, events=events)


def _audit_stage(
    session,
    run,
    specs,
    llm,
    active_rules,
    golden_records,
    plans=None,
    concurrency: int = 4,
    bus=None,
    events: Optional[List[tuple]] = None,
    blob_store: Optional[LocalBlobStore] = None,
    heal_report: Optional[Dict[str, Any]] = None,
):
    _, field_code_sets = load_published_code_sets(session, specs)
    outer_workers = max(1, min(concurrency, len(specs)))

    def worker(spec):
        table_rules = [rule for rule in active_rules if rule.table_code == spec.table_code]
        active_keys = {rule.rule_key for rule in table_rules}
        rule_index = {rule.rule_key: (rule.field, rule.operator, rule.params) for rule in table_rules}
        golden_record = golden_records[spec.table_code]
        mutations_per_rule = int(((plans or {}).get(spec.table_code) or {}).get("mutations_per_rule", 1))

        heal_target_keys = set()
        if heal_report:
            for case in heal_report.get("cases", []):
                if case.get("table_code") == spec.table_code:
                    heal_target_keys.update(case.get("missing_rules") or [])

        gaps: List[Dict[str, Any]] = []
        deterministic_cases = [build_golden_audit_case()]
        ai_target_rules: List[RuleDefinition] = []
        det_rule_count = 0

        # 确定性变异引擎先行：覆盖 GOLDEN 基线及 REQUIRED/LENGTH_MAX/ENUM/NOT_IN/NUM_RANGE/FIELD_COMPARE/COND_REQUIRED/FOREIGN_KEY
        for rule in table_rules:
            if rule.rule_key not in heal_target_keys:
                det_mutations = build_deterministic_mutations(
                    spec,
                    rule,
                    golden_record,
                    field_code_sets=field_code_sets,
                    mutations_per_rule=mutations_per_rule,
                )
                if det_mutations:
                    try:
                        validated = bind_audit_cases(
                            spec,
                            [item.model_dump() for item in det_mutations],
                            active_keys,
                            rule_index,
                            golden_record=golden_record,
                        )
                        deterministic_cases.extend(validated)
                        det_rule_count += 1
                        continue
                    except (RuleValidationError, ValueError):
                        pass
            ai_target_rules.append(rule)

        def bind_cases(target_rule):
            try:
                return _bind_one_case(target_rule)
            except WorkflowError as exc:
                fallback = build_regexp_fallback_mutation(spec, target_rule)
                if fallback is not None:
                    try:
                        validated = bind_audit_cases(
                            spec,
                            [fallback.model_dump()],
                            active_keys,
                            rule_index,
                            golden_record=golden_record,
                        )
                        return validated, {
                            "tool_name": "assign_audit_cases",
                            "strategy": "REGEXP_FALLBACK_MUTATION",
                            "target": target_rule.rule_key,
                            "fallback_from": str(exc),
                        }
                    except (RuleValidationError, ValueError):
                        pass
                gaps.append({"target": target_rule.rule_key, "error": str(exc)})
                return [], {
                    "tool_name": "assign_audit_cases",
                    "skipped": True,
                    "target": target_rule.rule_key,
                    "error": str(exc),
                }

        def _bind_one_case(target_rule):
            payload = {
                "table": spec_payload(spec),
                "golden_record": golden_record,
                "active_rules": [rule.model_dump() for rule in table_rules],
                "target_rule": target_rule.model_dump(),
                "generation_plan": (plans or {}).get(spec.table_code) or {"mutations_per_rule": 1},
                "published_code_sets": {
                    field.name: _code_set_context(field_code_sets, spec.table_code, field.name)["published_code_set"]
                    for field in spec.fields
                    if (spec.table_code, field.name) in field_code_sets
                },
            }
            if target_rule.rule_key in heal_target_keys:
                payload["previous_missed_cases"] = [
                    case
                    for case in (heal_report or {}).get("cases", [])
                    if target_rule.rule_key in (case.get("missing_rules") or [])
                ]
            instruction = (
                "针对 target_rule 按 generation_plan.mutations_per_rule 设计单点变异用例。"
                "expected_rule_keys 至少要包含 target_rule 自身（这条用例必须打死它）；"
                "同一变异值顺带触发的其它规则不必全部列出，也不要把它们当成额外义务。"
                "变异值必须是真正违规的取值（例如置空用 null、超长要真的超过声明长度、正则要构造贴近业务但明确不符合格式的反例），"
                "用空格或制表符冒充空值不算违规。ENUM 负样本应使用 published_code_sets 之外的明确非法值。"
            )
            return _call_validated(
                llm,
                "assign_audit_cases",
                instruction,
                payload,
                lambda args: bind_audit_cases(spec, args.get("cases", []), active_keys, rule_index, golden_record=golden_record),
                bus=bus,
            )

        ai_results = []
        if ai_target_rules:
            with ThreadPoolExecutor(max_workers=max(1, min(_inner_width(concurrency, outer_workers), len(ai_target_rules)))) as audit_executor:
                ai_results = list(audit_executor.map(bind_cases, ai_target_rules))

        case_items = []
        case_ids = set()
        traces: List[Dict[str, Any]] = [
            {
                "strategy": "DETERMINISTIC_MUTATIONS",
                "rule_count": det_rule_count,
                "case_count": len(deterministic_cases),
            }
        ]
        for case in deterministic_cases:
            case.case_id = normalize_case_id(case.case_id)
            if case.case_id in case_ids:
                case.case_id = normalize_case_id(f"{case.case_id}_{len(case_ids) + 1}")
            case_ids.add(case.case_id)
            case_items.append(case)
        for cases, trace in ai_results:
            traces.append(trace)
            for case in cases:
                case.case_id = normalize_case_id(case.case_id)
                if case.case_id in case_ids:
                    case.case_id = normalize_case_id(f"{case.case_id}_{len(case_ids) + 1}")
                case_ids.add(case.case_id)
                case_items.append(case)
        ai_traces = [tr for tr in traces if tr.get("strategy") not in {"DETERMINISTIC_MUTATIONS", "REGEXP_FALLBACK_MUTATION"}]
        first_ai = ai_traces[0] if ai_traces else None
        return case_items, {
            "provider": _provider_name(llm, first_ai) if first_ai else "DETERMINISTIC",
            "model": first_ai.get("model") if first_ai else "mutation-engine",
            "tool_name": "assign_audit_cases",
            "batch_count": len(traces),
            "deterministic_case_count": len(deterministic_cases),
            "ai_rule_count": len(ai_target_rules),
            "batches": traces,
            "gaps": gaps,
            "arguments": {"cases": [case.model_dump() for case in case_items]},
        }

    results = _fanout(session, run, "audit_case_binder", specs, worker, max_workers=concurrency, events=events, bus=bus)
    audit_gaps = {
        spec.table_code: trace["gaps"]
        for spec, (_, trace) in zip(specs, results)
        if trace.get("gaps")
    }
    if audit_gaps:
        _emit(
            events,
            session,
            run,
            "AUDIT_CASE_GAPS",
            "audit_case_binder",
            {
                "total": sum(len(items) for items in audit_gaps.values()),
                "tables": audit_gaps,
                "note": "这些规则没有生成对应的变异用例，审计覆盖会不完整",
            },
        )
    cases = {spec.table_code: result for spec, (result, _) in zip(specs, results)}
    for spec in specs:
        session.add(
            AuditCaseDefinitionModel(
                id=str(uuid.uuid4()),
                run_id=run.id,
                table_code=spec.table_code,
                version=_next_asset_version(session, AuditCaseDefinitionModel, run.id, spec.table_code),
                definition_json={"cases": [case.model_dump() for case in cases[spec.table_code]]},
            )
        )
    session.flush()
    cases_payload = {code: [item.model_dump() for item in items] for code, items in cases.items()}
    event_payload: Dict[str, Any] = {"tables": cases_payload}
    if blob_store is not None:
        event_payload["tables_blob"] = blob_store.put(canonical_json(cases_payload).encode("utf-8"))
    _emit(
        events,
        session,
        run,
        "AUDIT_CASES_ASSIGNED",
        "audit_barrier",
        event_payload,
    )
    _emit(
        events,
        session,
        run,
        "BSP_BARRIER_JOIN",
        "audit_barrier",
        {"stage": "audit_case_binder", "table_codes": list(cases)},
    )
    return cases


def _dedupe_approved_rules(session: Session, run: RunModel, events: Optional[List[tuple]] = None) -> int:
    """对同批次的 APPROVED 与 PENDING 规则统一做语义去重（APPROVED 优先保留，避免重复 PENDING 干扰人工确权）。"""
    rows = (
        session.query(RuleModel)
        .filter(RuleModel.run_id == run.id, RuleModel.status.in_(["APPROVED", "PENDING"]))
        .order_by(
            # APPROVED 排在 PENDING 之前，DOC_EXPLICIT 排在 AI_EXPANDED 之前
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
    # 只修「该命中却没命中」的规则。黄金用例上的误报（unexpected_rules）不进自动修正：
    # 让 AI 去「调到不报」等于允许它把规则改弱以通过测试，那条规则就废了，
    # 而且原文证据还挂着，事后完全看不出来。误报交给人判断。
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
                # 同 key 换 operator 等于把一条有原文证据的规则偷偷换成另一条：
                # 实测出现过 WABACHANDATE_REQUIRED 被改成 REGEXP，必填检查就此消失，
                # 而 rule_key 和证据页码还留着，事后无法从审计报告看出被替换过。
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
