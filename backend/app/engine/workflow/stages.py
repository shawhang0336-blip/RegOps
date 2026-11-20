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
    materialize_golden_record,
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
    _backfill_fixed_value_rules as _policy_backfill_fixed_value_rules,
    _backfill_length_rules as _policy_backfill_length_rules,
    _backfill_required_rules as _policy_backfill_required_rules,
    _declaration_quote,
    _fixed_value_candidates,
    _length_backfill_candidates,
    _required_backfill_candidates,
)


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
    def worker(spec):
        return _call_validated(
            llm,
            "assign_schema",
            "为每个白名单字段赋予物理存储类型和长度。必须逐字段全覆盖，不得改变数值字段语义。",
            spec_payload(spec),
            lambda args: bind_schema(spec, args.get("fields", [])),
            bus=bus,
        )

    results = _fanout(session, run, "schema_mapper", specs, worker, max_workers=concurrency, events=events, bus=bus)
    schemas = {spec.table_code: result for spec, (result, _) in zip(specs, results)}
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
    event_payload: Dict[str, Any] = {"tables": tables_payload}
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


def _rules_stage(session, run, specs, llm, plans=None, concurrency: int = 4, bus=None, events: Optional[List[tuple]] = None):
    _, field_code_sets = load_published_code_sets(session, specs)
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
            field_rules = []
            traces = []
            for _ in range(3):
                payload = {
                    **spec_payload(spec),
                    "fields": [field.model_dump()],
                    "field_names": [item.name for item in spec.fields],
                    "generation_plan": (plans or {}).get(spec.table_code) or {"mutations_per_rule": 1},
                    "existing_rule_keys": [rule.rule_key for rule in field_rules],
                    **_code_set_context(field_code_sets, spec.table_code, field.name),
                }
                rules, trace = _call_validated(
                    llm,
                    "assign_rules",
                    "根据生成计划返回本字段下一条最重要的规则，最多一条；已有规则不得重复，没有更多可靠规则就返回空数组。显性原文与推断拓展必须分开。若提供 published_code_set，ENUM 参数只能取其 values 白名单。COND_REQUIRED 的 if_field 必须严格取自输入 field_names 列表，不得使用简称或臆造字段名。数值上下限用 NUM_RANGE（params: min/max/min_inclusive/max_inclusive）；跨字段比较用 FIELD_COMPARE（params: right_field 必须取自 field_names，operator 取 LT/LE/GT/GE/EQ/NE，表示左字段必须满足的关系，如到期日>=起息日写 GE）。",
                    payload,
                    lambda args: bind_rules(spec, args.get("rules", [])),
                    bus=bus,
                )
                traces.append(trace)
                if not rules:
                    break
                field_rules.extend(rules)
            return field_rules, {
                "provider": "GLM",
                "model": traces[0]["model"] if traces else "unknown",
                "tool_name": "assign_rules",
                "rounds": traces,
                "arguments": {"rules": [rule.model_dump() for rule in field_rules]},
            }

        with ThreadPoolExecutor(max_workers=min(_inner_width(concurrency, outer_workers), len(fields))) as field_executor:
            field_results = list(field_executor.map(bind_field, fields))
        for rules, trace in field_results:
            bound_rules.extend(rules)
            batch_traces.append(trace)
        key_counts = {}
        for rule in bound_rules:
            key_counts[rule.rule_key] = key_counts.get(rule.rule_key, 0) + 1
            if key_counts[rule.rule_key] > 1:
                rule.rule_key = f"{rule.rule_key}_{key_counts[rule.rule_key]}"
                rule.rule_hash = calculate_rule_hash(rule, spec.source_sha256)
        if not bound_rules and gaps:
            raise WorkflowError(
                f"{spec.table_code} 的 {len(gaps)} 个字段全部未能绑定规则，"
                f"首个失败：{gaps[0]['error']}",
                context={"stage": "rule_binder", "table_code": spec.table_code, "field": gaps[0]["field"]},
            )
        return bound_rules, {
            "provider": "GLM",
            "model": (batch_traces[0].get("model") or "unknown") if batch_traces else "unknown",
            "tool_name": "assign_rules",
            "batch_count": len(batch_traces),
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
        payload = {
            "tables": [spec_payload(spec) for spec in specs],
            "table_codes": sorted(selected),
            "instruction": "只装配有明确依据的跨表 FOREIGN_KEY 规则，并给必要的黄金记录引用值覆盖。",
        }

        def cross_validator(args):
            bound = []
            for item in args.get("rules", []):
                target = next((spec for spec in specs if spec.table_code == item.get("table_code")), None)
                if not target:
                    raise RuleValidationError("Cross rule target table is outside the confirmed set")
                bound.extend(bind_rules(target, [item], selected))
            overrides = args.get("golden_overrides", {})
            if any(code not in selected for code in overrides):
                raise RuleValidationError("Golden override references an unconfirmed table")
            return (bound, overrides)

        (cross_rules, golden_overrides), trace = _call_validated(
            llm,
            "assign_rules",
            "装配跨表 FOREIGN_KEY 规则。rule 必须额外带 table_code；references_table 必须来自已确认表。可返回 golden_overrides 对齐引用值。",
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


def _data_stage(session, run, specs, llm, active_rules, golden_overrides, plans=None, concurrency: int = 4, bus=None, events: Optional[List[tuple]] = None, blob_store: Optional[LocalBlobStore] = None):
    _, field_code_sets = load_published_code_sets(session, specs)
    outer_workers = max(1, min(concurrency, len(specs)))

    def worker(spec):
        rule_views = [rule.model_dump() for rule in active_rules if rule.table_code == spec.table_code]

        def bind_field(field):
            payload = {
                **spec_payload(spec),
                "fields": [field.model_dump()],
                "approved_rules": rule_views,
                "required_reference_values": golden_overrides.get(spec.table_code, {}),
                "generation_plan": plans.get(spec.table_code) if plans else None,
                **_code_set_context(field_code_sets, spec.table_code, field.name),
            }

            def validate_one(args):
                items = args.get("generators", [])
                if len(items) != 1 or items[0].get("field") != field.name:
                    raise RuleValidationError("Generator response must contain exactly the requested field")
                # 当场做单字段校验（数值字段被塞 "N/A" 之类），让 _call_validated 立刻退避重绑，
                # 而不是等整表字段并发跑完才在 bind_generators 里失败
                binding = GeneratorBinding.model_validate(items[0])
                _validate_generator_item(field, binding)
                items[0]["params"] = binding.params
                return items[0]

            return _call_validated(
                llm,
                "assign_generators",
                "只为输入的一个字段赋予黄金合规生成器。FIXED 使用 params.value；ENUM 使用 params.values。必须满足相关规则和引用值。若提供 published_code_set，ENUM 的 values 只能是其子集。",
                payload,
                validate_one,
                bus=bus,
            )

        with ThreadPoolExecutor(max_workers=min(_inner_width(concurrency, outer_workers), len(spec.fields))) as field_executor:
            field_results = list(field_executor.map(bind_field, spec.fields))
        generator_items = [item for item, _ in field_results]
        generators = bind_generators(spec, generator_items)
        return generators, {
            "provider": "GLM",
            "model": field_results[0][1]["model"],
            "tool_name": "assign_generators",
            "batch_count": len(field_results),
            "batches": [trace for _, trace in field_results],
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


def _audit_stage(session, run, specs, llm, active_rules, golden_records, plans=None, concurrency: int = 4, bus=None, events: Optional[List[tuple]] = None, blob_store: Optional[LocalBlobStore] = None):
    _, field_code_sets = load_published_code_sets(session, specs)
    outer_workers = max(1, min(concurrency, len(specs)))

    def worker(spec):
        table_rules = [rule for rule in active_rules if rule.table_code == spec.table_code]
        active_keys = {rule.rule_key for rule in table_rules}
        rule_index = {rule.rule_key: (rule.field, rule.operator, rule.params) for rule in table_rules}

        gaps: List[Dict[str, Any]] = []

        def bind_cases(target_rule=None):
            try:
                return _bind_one_case(target_rule)
            except WorkflowError as exc:
                label = target_rule.rule_key if target_rule else "GOLDEN"
                gaps.append({"target": label, "error": str(exc)})
                if target_rule is None:
                    # 黄金用例是整表基线，缺了就没法判"无违规"这一侧
                    raise
                return [], {
                    "tool_name": "assign_audit_cases",
                    "skipped": True,
                    "target": label,
                    "error": str(exc),
                }

        def _bind_one_case(target_rule=None):
            payload = {
                "table": spec_payload(spec),
                "golden_record": golden_records[spec.table_code],
                "active_rules": [rule.model_dump() for rule in table_rules],
                "target_rule": target_rule.model_dump() if target_rule else None,
                "generation_plan": (plans or {}).get(spec.table_code) or {"mutations_per_rule": 1},
                "published_code_sets": {
                    field.name: _code_set_context(field_code_sets, spec.table_code, field.name)["published_code_set"]
                    for field in spec.fields
                    if (spec.table_code, field.name) in field_code_sets
                },
            }
            instruction = (
                "针对 target_rule 按 generation_plan.mutations_per_rule 设计单点变异用例。"
                "expected_rule_keys 至少要包含 target_rule 自身（这条用例必须打死它）；"
                "同一变异值顺带触发的其它规则不必全部列出，也不要把它们当成额外义务。"
                "变异值必须是真正违规的取值（例如置空用 null、超长要真的超过声明长度），"
                "用空格或制表符冒充空值不算违规。ENUM 负样本应使用 published_code_sets 之外的明确非法值。"
                if target_rule
                else "设计一个无覆盖的黄金合规审计用例，expected_rule_keys 必须为空（黄金用例要求零命中）。"
            )
            return _call_validated(
                llm,
                "assign_audit_cases",
                instruction,
                payload,
                lambda args: bind_audit_cases(spec, args.get("cases", []), active_keys, rule_index, golden_record=golden_records.get(spec.table_code)),
                bus=bus,
            )

        targets = [None] + table_rules
        with ThreadPoolExecutor(max_workers=min(_inner_width(concurrency, outer_workers), len(targets))) as audit_executor:
            audit_results = list(audit_executor.map(bind_cases, targets))
        case_items = []
        case_ids = set()
        traces = []
        for cases, trace in audit_results:
            traces.append(trace)
            for case in cases:
                if case.case_id in case_ids:
                    case.case_id = f"{case.case_id}_{len(case_ids) + 1}"
                case_ids.add(case.case_id)
                case_items.append(case)
        return case_items, {
            "provider": "GLM",
            "model": traces[0].get("model") if traces else "unknown",
            "tool_name": "assign_audit_cases",
            "batch_count": len(traces),
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
    rows = (
        session.query(RuleModel)
        .filter(RuleModel.run_id == run.id, RuleModel.status == "APPROVED")
        .order_by(RuleModel.source.desc(), RuleModel.created_at, RuleModel.rule_key)
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
