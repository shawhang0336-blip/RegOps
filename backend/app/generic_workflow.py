import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy import func
from sqlalchemy.orm import Session

from .blobs import LocalBlobStore
from .compiler import compile_rule_query
from .asset_models import ApprovalModel, ArtifactModel, AuditCaseDefinitionModel, GenerationPlanModel, GeneratorDefinitionModel, SchemaDefinitionModel
from .config import Settings
from .domain import RuleDefinition, SpecDocument, calculate_rule_hash, canonical_json
from .events import append_event
from .generic_ai import RealDesignLlm, spec_payload
from .generic_domain import (
    bind_audit_cases,
    bind_generators,
    bind_rules,
    bind_schema,
    materialize_golden_record,
)
from .generic_oracle import GenericOracleExecutor, compile_ai_schema_mysql_ddl, compile_ai_schema_oracle_ddl
from .models import EventModel, RuleModel, RunModel, SpecificationModel
from .rules import RuleValidationError
from .workflow import WorkflowError, _pending_rules, _upsert_rule


def _specs(session: Session, spec_id: str) -> List[SpecDocument]:
    row = session.get(SpecificationModel, spec_id)
    if not row or "tables" not in row.content_json:
        raise WorkflowError("Confirmed specification snapshot not found")
    if not row.content_json.get("selection_confirmed"):
        raise WorkflowError("Tables must be confirmed before any AI binding or DDL execution")
    return [SpecDocument.model_validate(item) for item in row.content_json["tables"]]


def _call_validated(llm, tool_name, instruction, payload, validator):
    feedback = None
    rejection = None
    for _ in range(2):
        try:
            arguments, trace = llm.assign(tool_name, instruction, payload, feedback)
            result = validator(arguments)
            if rejection:
                trace["previous_rejection"] = rejection
            return result, trace
        except (httpx.HTTPError, RuntimeError, RuleValidationError, ValueError, KeyError, TypeError) as exc:
            rejection = str(exc)
            feedback = rejection
    raise WorkflowError(f"{tool_name} failed deterministic validation twice: {rejection}")


def _fanout(session: Session, run: RunModel, stage: str, specs: List[SpecDocument], worker) -> List[Any]:
    append_event(
        session,
        run,
        "BSP_FANOUT",
        stage,
        {"stage": stage, "table_codes": [spec.table_code for spec in specs]},
    )
    with ThreadPoolExecutor(max_workers=min(4, len(specs))) as executor:
        futures = [executor.submit(worker, spec) for spec in specs]
        results = [future.result() for future in futures]
    for spec, (_, trace) in zip(specs, results):
        append_event(
            session,
            run,
            "AI_TOOL_ASSIGNED",
            stage,
            {"table_code": spec.table_code, **trace},
        )
    return results


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


def _plan_stage(session, run, specs, llm, budget):
    def worker(spec):
        return _call_validated(
            llm,
            "assign_generation_plan",
            "根据表复杂度自行决定必做规则数、拓展规则数、黄金数据量和每规则变异数，并说明理由；不得超出输入预算。",
            {**spec_payload(spec), "budget": budget},
            lambda args: _validate_plan(args, budget),
        )

    results = _fanout(session, run, "generation_planner", specs, worker)
    plans = {spec.table_code: result for spec, (result, _) in zip(specs, results)}
    append_event(
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
    append_event(session, run, "BSP_BARRIER_JOIN", "plan_barrier", {"stage": "generation_planner", "table_codes": list(plans)})
    return plans


def _schema_stage(session, run, specs, llm):
    def worker(spec):
        return _call_validated(
            llm,
            "assign_schema",
            "为每个白名单字段赋予物理存储类型和长度。必须逐字段全覆盖，不得改变数值字段语义。",
            spec_payload(spec),
            lambda args: bind_schema(spec, args.get("fields", [])),
        )

    results = _fanout(session, run, "schema_mapper", specs, worker)
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
    append_event(
        session,
        run,
        "SCHEMA_ASSIGNED",
        "schema_barrier",
        {"tables": {code: [item.model_dump() for item in items] for code, items in schemas.items()}},
    )
    append_event(
        session,
        run,
        "BSP_BARRIER_JOIN",
        "schema_barrier",
        {"stage": "schema_mapper", "table_codes": list(schemas)},
    )
    return schemas


def _rules_stage(session, run, specs, llm, plans=None) -> int:
    def worker(spec):
        bound_rules = []
        batch_traces = []
        fields = spec.fields

        def bind_field(field):
            field_rules = []
            traces = []
            for _ in range(3):
                payload = {
                    **spec_payload(spec),
                    "fields": [field.model_dump()],
                    "generation_plan": (plans or {}).get(spec.table_code) or {"mutations_per_rule": 1},
                    "existing_rule_keys": [rule.rule_key for rule in field_rules],
                }
                rules, trace = _call_validated(
                    llm,
                    "assign_rules",
                    "根据生成计划返回本字段下一条最重要的规则，最多一条；已有规则不得重复，没有更多可靠规则就返回空数组。显性原文与推断拓展必须分开。",
                    payload,
                    lambda args: bind_rules(spec, args.get("rules", [])),
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

        with ThreadPoolExecutor(max_workers=min(4, len(fields))) as field_executor:
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
        return bound_rules, {
            "provider": "GLM",
            "model": batch_traces[0]["model"] if batch_traces else "unknown",
            "tool_name": "assign_rules",
            "batch_count": len(batch_traces),
            "batches": batch_traces,
            "arguments": {"rules": [rule.model_dump() for rule in bound_rules]},
        }

    results = _fanout(session, run, "rule_binder", specs, worker)
    for spec, (rules, _) in zip(specs, results):
        for rule in rules:
            _upsert_rule(session, run, rule, "APPROVED" if rule.source == "DOC_EXPLICIT" else "PENDING")
    append_event(
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
        )
        append_event(session, run, "AI_TOOL_ASSIGNED", "cross_rule_binder", trace)
        for rule in cross_rules:
            _upsert_rule(session, run, rule, "APPROVED" if rule.source == "DOC_EXPLICIT" else "PENDING")
    append_event(
        session,
        run,
        "CROSS_RULES_ASSIGNED",
        "cross_rule_binder",
        {"rule_keys": [rule.rule_key for rule in cross_rules], "golden_overrides": golden_overrides},
    )
    bound = session.query(RuleModel).filter(RuleModel.run_id == run.id).all()
    pending_count = len(_pending_rules(session, run.id))
    append_event(
        session,
        run,
        "RULES_BOUND",
        "rule_barrier",
        {
            "pending_count": pending_count,
            "rule_set": [
                {"rule_key": rule.rule_key, "rule_hash": rule.rule_hash, "source": rule.source, "table_code": rule.table_code}
                for rule in sorted(bound, key=lambda item: (item.table_code, item.rule_key))
            ],
        },
    )
    return pending_count


def _next_asset_version(session, model, run_id, table_code) -> int:
    current = session.query(func.max(model.version)).filter(model.run_id == run_id, model.table_code == table_code).scalar()
    return int(current or 0) + 1


def _data_stage(session, run, specs, llm, active_rules, golden_overrides, plans=None):
    def worker(spec):
        rule_views = [rule.model_dump() for rule in active_rules if rule.table_code == spec.table_code]

        def bind_field(field):
            payload = {
                **spec_payload(spec),
                "fields": [field.model_dump()],
                "approved_rules": rule_views,
                "required_reference_values": golden_overrides.get(spec.table_code, {}),
                "generation_plan": plans.get(spec.table_code),
            }

            def validate_one(args):
                items = args.get("generators", [])
                if len(items) != 1 or items[0].get("field") != field.name:
                    raise RuleValidationError("Generator response must contain exactly the requested field")
                return items[0]

            return _call_validated(
                llm,
                "assign_generators",
                "只为输入的一个字段赋予黄金合规生成器。FIXED 使用 params.value；ENUM 使用 params.values。必须满足相关规则和引用值。",
                payload,
                validate_one,
            )

        with ThreadPoolExecutor(max_workers=min(4, len(spec.fields))) as field_executor:
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

    results = _fanout(session, run, "data_generator_binder", specs, worker)
    generators = {spec.table_code: result for spec, (result, _) in zip(specs, results)}
    golden_records = {
        spec.table_code: materialize_golden_record(spec, generators[spec.table_code]) for spec in specs
    }
    for table_code, overrides in golden_overrides.items():
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
    append_event(
        session,
        run,
        "GENERATORS_ASSIGNED",
        "data_barrier",
        {
            "tables": {
                spec.table_code: {
                    "generators": [item.model_dump() for item in generators[spec.table_code]],
                    "golden_record": golden_records[spec.table_code],
                }
                for spec in specs
            }
        },
    )
    append_event(
        session,
        run,
        "BSP_BARRIER_JOIN",
        "data_barrier",
        {"stage": "data_generator_binder", "table_codes": list(generators)},
    )
    return golden_records


def _audit_stage(session, run, specs, llm, active_rules, golden_records, plans=None):
    def worker(spec):
        table_rules = [rule for rule in active_rules if rule.table_code == spec.table_code]
        active_keys = {rule.rule_key for rule in table_rules}

        def bind_cases(target_rule=None):
            payload = {
                "table": spec_payload(spec),
                "golden_record": golden_records[spec.table_code],
                "active_rules": [rule.model_dump() for rule in table_rules],
                "target_rule": target_rule.model_dump() if target_rule else None,
                "generation_plan": (plans or {}).get(spec.table_code) or {"mutations_per_rule": 1},
            }
            instruction = (
                "针对 target_rule 按 generation_plan.mutations_per_rule 设计单点变异用例，expected_rule_keys 精确列出全部预期命中。"
                if target_rule
                else "设计一个无覆盖的黄金合规审计用例，expected_rule_keys 应为空。"
            )
            return _call_validated(
                llm,
                "assign_audit_cases",
                instruction,
                payload,
                lambda args: bind_audit_cases(spec, args.get("cases", []), active_keys),
            )

        targets = [None] + table_rules
        with ThreadPoolExecutor(max_workers=min(4, len(targets))) as audit_executor:
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
            "model": traces[0]["model"],
            "tool_name": "assign_audit_cases",
            "batch_count": len(traces),
            "batches": traces,
            "arguments": {"cases": [case.model_dump() for case in case_items]},
        }

    results = _fanout(session, run, "audit_case_binder", specs, worker)
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
    append_event(
        session,
        run,
        "AUDIT_CASES_ASSIGNED",
        "audit_barrier",
        {"tables": {code: [item.model_dump() for item in items] for code, items in cases.items()}},
    )
    append_event(
        session,
        run,
        "BSP_BARRIER_JOIN",
        "audit_barrier",
        {"stage": "audit_case_binder", "table_codes": list(cases)},
    )
    return cases


def _rebind_failed_rules(session, run, specs, llm, report) -> int:
    failed_keys = sorted(
        {
            rule_key
            for case in report["cases"]
            for rule_key in case["missing_rules"] + case["unexpected_rules"]
        }
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
        failed_cases = [
            case
            for case in report["cases"]
            if row.rule_key in case["missing_rules"] or row.rule_key in case["unexpected_rules"]
        ]
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
            revised[0].rule_key = current.rule_key
            revised[0].rule_hash = calculate_rule_hash(revised[0], spec.source_sha256)
            return revised[0]

        revised, trace = _call_validated(
            llm,
            "assign_rules",
            "根据失败记录修正当前规则赋值。最大长度用 LENGTH_MAX，精确长度用 LENGTH；只返回修正后的一条规则。",
            payload,
            validate_revision,
        )
        append_event(
            session,
            run,
            "AI_TOOL_ASSIGNED",
            "rule_binder",
            {"table_code": spec.table_code, "rebind_for": current.rule_key, **trace},
        )
        row.status = "SUPERSEDED"
        _upsert_rule(session, run, revised, "APPROVED" if revised.source == "DOC_EXPLICIT" else "PENDING")
        revised_count += 1
    return revised_count


def retry_generic_run(session: Session, run: RunModel, settings: Settings) -> RunModel:
    if not run.result_json or not run.result_json.get("audit_report_blob"):
        raise WorkflowError("This failure has no persisted audit report to repair")
    specs = _specs(session, run.spec_id)
    llm = RealDesignLlm(settings)
    report_uri = run.result_json["audit_report_blob"]
    report = json.loads(LocalBlobStore(settings.blob_dir).get(report_uri).decode("utf-8"))
    append_event(
        session,
        run,
        "GO_REDIRECT",
        "human_retry",
        {"target": "rule_binder", "reason": "Repair rules from the persisted failed audit before regenerating data"},
    )
    revised = _rebind_failed_rules(session, run, specs, llm, report)
    if not revised:
        raise WorkflowError("No failed active rules could be revised")
    pending_count = len(_pending_rules(session, run.id))
    if pending_count:
        append_event(session, run, "APPROVAL_REQUIRED", "human_gate", {"pending_count": pending_count})
        session.commit()
        return run
    session.commit()
    return continue_generic_run(session, run, settings, llm)


def _latest_payload(session: Session, run_id: str, kind: str) -> Dict[str, Any]:
    event = (
        session.query(EventModel)
        .filter(EventModel.run_id == run_id, EventModel.kind == kind)
        .order_by(EventModel.seq.desc())
        .first()
    )
    if not event:
        raise WorkflowError(f"Missing event payload: {kind}")
    return event.payload_json


def start_generic_run(session: Session, spec_id: str, settings: Settings, llm=None, generation_budget=None) -> RunModel:
    specs = _specs(session, spec_id)
    llm = llm or RealDesignLlm(settings)
    run = RunModel(id=str(uuid.uuid4()), spec_id=spec_id, status="NEW", current_node="created")
    session.add(run)
    session.flush()
    specification = session.get(SpecificationModel, spec_id)
    append_event(
        session,
        run,
        "RUN_CREATED",
        "table_confirmation",
        {"spec_id": spec_id, "provider": "REAL_GLM", "confirmation": specification.content_json.get("confirmation")},
    )
    try:
        plans = _plan_stage(session, run, specs, llm, generation_budget) if generation_budget else None
        _schema_stage(session, run, specs, llm)
        pending_count = _rules_stage(session, run, specs, llm, plans)
    except Exception as exc:
        append_event(
            session,
            run,
            "RUN_FAILED",
            run.current_node,
            {"node": run.current_node, "result": {"error": f"{type(exc).__name__}: {exc}"}},
        )
        session.commit()
        return run
    if pending_count:
        append_event(
            session,
            run,
            "APPROVAL_REQUIRED",
            "human_gate",
            {"pending_rule_hashes": [rule.rule_hash for rule in _pending_rules(session, run.id)]},
        )
        session.commit()
        return run
    session.commit()
    return continue_generic_run(session, run, settings, llm)


def approve_generic_rules(
    session: Session,
    run: RunModel,
    approver: str,
    decisions: Dict[str, str],
    settings: Settings,
    llm=None,
) -> RunModel:
    if run.status != "WAITING_APPROVAL":
        raise WorkflowError("Run is not waiting for rule approval")
    pending = {rule.rule_hash: rule for rule in _pending_rules(session, run.id)}
    if not decisions or any(rule_hash not in pending for rule_hash in decisions):
        raise WorkflowError("Decisions must target currently pending rules")
    normalized = []
    for rule_hash, decision in decisions.items():
        if decision not in {"APPROVED", "REJECTED"}:
            raise WorkflowError("Decision must be APPROVED or REJECTED")
        pending[rule_hash].status = decision
        normalized.append({"rule_hash": rule_hash, "decision": decision})
        session.add(
            ApprovalModel(
                id=str(uuid.uuid4()),
                object_type="RULE",
                object_hash=rule_hash,
                decision=decision,
                approver=approver,
                comment="",
            )
        )
    session.flush()
    all_decided = not _pending_rules(session, run.id)
    append_event(
        session,
        run,
        "HUMAN_DECISIONS_RECORDED",
        "human_gate",
        {"approver": approver, "decisions": normalized, "all_decided": all_decided},
    )
    session.commit()
    return continue_generic_run(session, run, settings, llm) if all_decided else run


def _continue_generic_run(session: Session, run: RunModel, settings: Settings, llm=None) -> RunModel:
    specs = _specs(session, run.spec_id)
    llm = llm or RealDesignLlm(settings)
    schema_payload = _latest_payload(session, run.id, "SCHEMA_ASSIGNED")["tables"]
    schemas = {code: [item for item in items] for code, items in schema_payload.items()}
    cross = _latest_payload(session, run.id, "CROSS_RULES_ASSIGNED")
    plan_event = (
        session.query(EventModel)
        .filter(EventModel.run_id == run.id, EventModel.kind == "GENERATION_PLANNED")
        .order_by(EventModel.seq.desc())
        .first()
    )
    plans = plan_event.payload_json["tables"] if plan_event else {}
    rows = session.query(RuleModel).filter(RuleModel.run_id == run.id, RuleModel.status == "APPROVED").all()
    rules = [RuleDefinition.model_validate(row.definition_json) for row in rows]
    golden_records = _data_stage(session, run, specs, llm, rules, cross.get("golden_overrides", {}), plans)
    audit_cases = _audit_stage(session, run, specs, llm, rules, golden_records, plans)
    attempt = run.attempt + 1
    append_event(session, run, "AUDIT_STARTED", "oracle_executor", {"attempt": attempt})
    schema_models = {code: [item for item in items] for code, items in schema_payload.items()}
    from .generic_domain import GeneratorBinding, SchemaBinding

    generator_payload = _latest_payload(session, run.id, "GENERATORS_ASSIGNED")["tables"]
    golden_generators = {
        code: [GeneratorBinding.model_validate(item) for item in values["generators"]]
        for code, values in generator_payload.items()
    }
    golden_counts = {code: int(plan.get("golden_count", 1)) for code, plan in plans.items()}
    typed_schemas = {
        code: [SchemaBinding.model_validate(item) for item in items] for code, items in schema_models.items()
    }
    report = GenericOracleExecutor(settings.oracle_sandbox_url).execute(
        run.id,
        attempt,
        specs,
        typed_schemas,
        golden_records,
        audit_cases,
        rules,
        golden_counts,
        golden_generators,
    )
    blobs = LocalBlobStore(settings.blob_dir)
    ddl_uri = blobs.put(
        "\n\n".join(compile_ai_schema_mysql_ddl(spec.table_code, typed_schemas[spec.table_code]) for spec in specs).encode()
    )
    physical_tables = report["physical_tables"]
    oracle_ddl_uri = blobs.put(
        "\n\n".join(
            compile_ai_schema_oracle_ddl(report["target_tables"][spec.table_code], typed_schemas[spec.table_code])
            for spec in specs
        ).encode()
    )
    queries = []
    for rule in rules:
        sql, params = compile_rule_query(rule, "oracle", physical_tables)
        queries.append({"rule_key": rule.rule_key, "table_code": rule.table_code, "sql": sql, "params": params})
    sql_uri = blobs.put(canonical_json({"dialect": "oracle", "queries": queries}).encode())
    report_uri = blobs.put(canonical_json(report).encode())
    result = {
        "ddl_blob": ddl_uri,
        "oracle_ddl_blob": oracle_ddl_uri,
        "validation_sql_blob": sql_uri,
        "audit_report_blob": report_uri,
        "oracle_tables": physical_tables,
        "oracle_target_tables": report["target_tables"],
        "active_rule_hashes": sorted(rule.rule_hash for rule in rules),
        "audit_summary": {key: value for key, value in report.items() if key not in {"cases", "physical_tables", "target_tables"}},
    }
    for artifact_type, uri in [
        ("MYSQL_DDL", ddl_uri),
        ("ORACLE_DDL", oracle_ddl_uri),
        ("VALIDATION_SQL", sql_uri),
        ("AUDIT_REPORT", report_uri),
    ]:
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
    session.flush()
    if not report["all_passed"] and attempt < 3:
        append_event(
            session,
            run,
            "GO_REDIRECT",
            "oracle_executor",
            {"target": "rule_binder", "failed_result": result},
        )
        revised = _rebind_failed_rules(session, run, specs, llm, report)
        if revised:
            pending_count = len(_pending_rules(session, run.id))
            if pending_count:
                append_event(session, run, "APPROVAL_REQUIRED", "human_gate", {"pending_count": pending_count})
                session.commit()
                return run
            return continue_generic_run(session, run, settings, llm)
    append_event(
        session,
        run,
        "AUDIT_COMPLETED" if report["all_passed"] else "AUDIT_REJECTED",
        "oracle_executor",
        {"result": result, "node": "oracle_executor"},
    )
    session.commit()
    return run


def continue_generic_run(session: Session, run: RunModel, settings: Settings, llm=None) -> RunModel:
    try:
        return _continue_generic_run(session, run, settings, llm)
    except Exception as exc:
        append_event(
            session,
            run,
            "RUN_FAILED",
            run.current_node,
            {"node": run.current_node, "result": {"error": f"{type(exc).__name__}: {exc}"}},
        )
        session.commit()
        return run
