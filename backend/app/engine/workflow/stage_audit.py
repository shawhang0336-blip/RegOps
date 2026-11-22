"""阶段二子模块 4：审计变异用例绑定（单表 BSP Worker + 波次屏障汇聚）。"""

import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from app.assets.code_sets.oracle import load_published_code_sets
from app.engine.binding import bind_audit_cases, normalize_case_id
from app.engine.mutations import (
    build_deterministic_mutations,
    build_golden_audit_case,
    build_regexp_fallback_mutation,
)
from app.engine.rules import RuleValidationError
from app.engine.workflow.runtime import _call_validated, _code_set_context, _emit, _fanout, _inner_width
from app.engine.workflow.stage_data import _next_asset_version
from app.engine.workflow.stage_plan_schema import _provider_name
from app.infra.models import AuditCaseDefinitionModel
from app.infra.blob_store import LocalBlobStore
from app.infra.domain import AuditCaseBinding, RuleDefinition, canonical_json
from app.infra.errors import WorkflowError
from app.infra.llm_schemas import spec_payload


def bind_table_audit_worker(
    spec,
    *,
    llm,
    active_rules: List[RuleDefinition],
    golden_record: Dict[str, Any],
    field_code_sets: Dict[Any, Any],
    plans: Optional[Dict[str, Any]] = None,
    concurrency: int = 4,
    outer_workers: int = 1,
    bus: Any = None,
    heal_report: Optional[Dict[str, Any]] = None,
) -> Tuple[List[AuditCaseBinding], Dict[str, Any]]:
    """纯单表审计变异用例生成 Worker（零 DB 写操作，可安全并发运行在 Scheduler BSP Send 波次内）。"""
    table_rules = [rule for rule in active_rules if rule.table_code == spec.table_code]
    active_keys = {rule.rule_key for rule in table_rules}
    rule_index = {rule.rule_key: (rule.field, rule.operator, rule.params) for rule in table_rules}
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

    ai_results = []
    if ai_target_rules:
        inner = max(1, min(_inner_width(concurrency, outer_workers), len(ai_target_rules)))
        if inner <= 1 or len(ai_target_rules) <= 1:
            ai_results = [bind_cases(rule) for rule in ai_target_rules]
        else:
            with ThreadPoolExecutor(max_workers=inner) as audit_executor:
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


def join_table_audit_barrier(
    session,
    run,
    specs,
    table_results: Dict[str, Dict[str, Any]],
    *,
    events: Optional[List[tuple]] = None,
    blob_store: Optional[LocalBlobStore] = None,
) -> Dict[str, List[AuditCaseBinding]]:
    """审计用例波次屏障汇聚：上报缺口 → 落库 AuditCaseDefinitionModel → 发射 AUDIT_CASES_ASSIGNED。"""
    audit_gaps = {
        spec.table_code: table_results[spec.table_code]["gaps"]
        for spec in specs
        if (table_results.get(spec.table_code) or {}).get("gaps")
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
    cases: Dict[str, List[AuditCaseBinding]] = {}
    for spec in specs:
        raw_cases = (table_results.get(spec.table_code) or {}).get("cases") or []
        cases[spec.table_code] = [
            item if isinstance(item, AuditCaseBinding) else AuditCaseBinding.model_validate(item)
            for item in raw_cases
        ]
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
    _emit(events, session, run, "AUDIT_CASES_ASSIGNED", "audit_barrier", event_payload)
    _emit(
        events,
        session,
        run,
        "BSP_BARRIER_JOIN",
        "audit_barrier",
        {"stage": "audit_case_binder", "table_codes": list(cases)},
    )
    return cases


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
        return bind_table_audit_worker(
            spec,
            llm=llm,
            active_rules=active_rules,
            golden_record=golden_records[spec.table_code],
            field_code_sets=field_code_sets,
            plans=plans,
            concurrency=concurrency,
            outer_workers=outer_workers,
            bus=bus,
            heal_report=heal_report,
        )

    results = _fanout(session, run, "audit_case_binder", specs, worker, max_workers=concurrency, events=events, bus=bus)
    table_results = {
        spec.table_code: {"cases": items, "gaps": trace.get("gaps") or [], "trace": trace}
        for spec, (items, trace) in zip(specs, results)
    }
    return join_table_audit_barrier(session, run, specs, table_results, events=events, blob_store=blob_store)
