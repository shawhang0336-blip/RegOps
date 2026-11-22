"""运行批次查询与审计明细聚合服务（供 `api/delivery/runs.py` 调用）。"""

import json
from typing import Dict, List

from sqlalchemy.orm import Session

from app.api.common import run_tables_from_content, run_view
from app.delivery.workflow import _regops_delete_pre_hook
from app.infra.blob_store import LocalBlobStore
from app.infra.config import Settings
from app.infra.db_tables import EventModel, RuleModel, RunModel, SpecificationModel
from app.policy.regops_channels import resolve_event_tables
from app.policy.regops_plan import project, project_many


def delete_run_rows(session: Session, run: RunModel):
    """兼容辅助：委托至 delivery 层统一删除前置钩子（供既有单测调用）。"""
    ctx: Dict = {}
    _regops_delete_pre_hook(session, run, ctx)
    session.query(EventModel).filter(EventModel.run_id == run.id).delete(synchronize_session=False)
    session.delete(run)
    return ctx.get("blob_uris", set()), ctx.get("oracle_tables", set())


def _latest_event_tables(events: List[EventModel], kind: str, blob_store: LocalBlobStore) -> Dict:
    tables: Dict = {}
    for event in events:
        if event.kind == kind:
            tables = resolve_event_tables(event.payload_json or {}, blob_store)
    return tables


def list_generation_runs(session: Session, *, limit: int = 50) -> List[Dict]:
    """仅列出 Step 2/3 规则验证批次（关联 SpecificationModel），与文档导入 Run 隔离。"""
    runs = (
        session.query(RunModel)
        .join(SpecificationModel, RunModel.spec_id == SpecificationModel.id)
        .order_by(RunModel.created_at.desc())
        .limit(limit)
        .all()
    )
    if not runs:
        return []
    states = project_many(session, runs)
    run_ids = [run.id for run in runs]
    spec_ids = {run.spec_id for run in runs}
    rules_by_run: Dict[str, List[RuleModel]] = {}
    for rule in session.query(RuleModel).filter(RuleModel.run_id.in_(run_ids)).all():
        rules_by_run.setdefault(rule.run_id, []).append(rule)
    spec_by_id = {
        row.id: row
        for row in session.query(SpecificationModel).filter(SpecificationModel.id.in_(spec_ids)).all()
    }
    return [
        run_view(
            session,
            run,
            include_events=False,
            run_state=states[run.id],
            prefetched_rules=rules_by_run.get(run.id, []),
            prefetched_tables=run_tables_from_content(
                (spec_by_id[run.spec_id].content_json or {}) if run.spec_id in spec_by_id else {}
            ),
        )
        for run in runs
    ]


def build_validation_detail(session: Session, run: RunModel, settings: Settings) -> Dict:
    """把 AI 测试数据定义和 Oracle 审计报告整理成可浏览、可逐规则核对的视图。"""
    blob_store = LocalBlobStore(settings.blob_dir)
    state = project(session, run)
    result = state.result_json or {}
    report: Dict = {}
    report_uri = result.get("audit_report_blob")
    if report_uri:
        try:
            report = json.loads(blob_store.get(report_uri).decode("utf-8"))
        except (ValueError, FileNotFoundError, json.JSONDecodeError):
            report = {}

    events = session.query(EventModel).filter(EventModel.run_id == run.id).order_by(EventModel.seq).all()
    generators = _latest_event_tables(events, "GENERATORS_ASSIGNED", blob_store)
    plans = _latest_event_tables(events, "GENERATION_PLANNED", blob_store)
    case_definitions = _latest_event_tables(events, "AUDIT_CASES_ASSIGNED", blob_store)
    case_definition_map = {
        (table_code, item.get("case_id")): item
        for table_code, items in case_definitions.items()
        for item in items
    }

    approved = session.query(RuleModel).filter(RuleModel.run_id == run.id, RuleModel.status == "APPROVED").all()
    rule_map: Dict = {}
    for row in approved:
        definition = row.definition_json or {}
        rule_map[(row.table_code, row.rule_key)] = {
            "rule_hash": row.rule_hash,
            "rule_key": row.rule_key,
            "table_code": row.table_code,
            "field": definition.get("field"),
            "operator": definition.get("operator"),
            "params": definition.get("params") or {},
            "source": row.source,
            "status": row.status,
            "rationale": definition.get("rationale") or "",
            "expected_case_ids": [],
            "hit_case_ids": [],
            "missing_case_ids": [],
            "unexpected_case_ids": [],
            "co_triggered_case_ids": [],
        }

    cases = []
    for raw in report.get("cases") or []:
        table_code = raw.get("table_code")
        case_id = raw.get("case_id")
        definition = case_definition_map.get((table_code, case_id), {})
        case = {
            **raw,
            "case_kind": raw.get("case_kind") or ("GOLDEN" if "GOLDEN" in str(case_id).upper() else "MUTATION"),
            "overrides": definition.get("overrides") or {},
            "expected_rule_keys": definition.get("expected_rule_keys") or raw.get("expected_rules") or [],
        }
        cases.append(case)
        for key in case.get("expected_rules") or []:
            rule = rule_map.get((table_code, key))
            if rule:
                rule["expected_case_ids"].append(case_id)
        for key in case.get("actual_rules") or []:
            rule = rule_map.get((table_code, key))
            if rule:
                rule["hit_case_ids"].append(case_id)
        for key in case.get("missing_rules") or []:
            rule = rule_map.get((table_code, key))
            if rule:
                rule["missing_case_ids"].append(case_id)
        for key in case.get("unexpected_rules") or []:
            rule = rule_map.get((table_code, key))
            if rule:
                rule["unexpected_case_ids"].append(case_id)
        for key in case.get("co_triggered_rules") or []:
            rule = rule_map.get((table_code, key))
            if rule:
                rule["co_triggered_case_ids"].append(case_id)

    rules = [
        {
            **rule,
            "covered": bool(rule["hit_case_ids"]),
            "has_target_case": bool(rule["expected_case_ids"]),
            "effective": bool(rule["hit_case_ids"]) and not bool(rule["missing_case_ids"] or rule["unexpected_case_ids"]),
            "result": (
                "FAILED"
                if (rule["missing_case_ids"] or rule["unexpected_case_ids"])
                else "EFFECTIVE"
                if rule["hit_case_ids"]
                else "UNCOVERED"
            ),
        }
        for rule in rule_map.values()
    ]
    rules.sort(key=lambda item: (item["table_code"], item["field"] or "", item["rule_key"]))

    spec = session.get(SpecificationModel, run.spec_id)
    spec_content = (spec.content_json or {}) if spec else {}
    spec_tables = spec_content.get("tables") or ([spec_content] if spec_content else [])
    table_names = {item.get("table_code"): item.get("table_name") for item in spec_tables}
    table_codes = sorted(set(result.get("oracle_tables") or {}) | set(generators) | set(table_names))
    test_data = []
    for table_code in table_codes:
        generator = generators.get(table_code) or {}
        plan = plans.get(table_code) or generator.get("generation_plan") or {}
        audit_count = sum(1 for case in cases if case.get("table_code") == table_code)
        test_data.append(
            {
                "table_code": table_code,
                "table_name": table_names.get(table_code) or table_code,
                "generation_mode": generator.get("generation_mode") or (
                    "HYBRID_DETERMINISTIC_AI" if generator else "DETERMINISTIC"
                ),
                "generator_count": len(generator.get("generators") or []),
                "golden_record": generator.get("golden_record") or {},
                "golden_count": int(plan.get("golden_count") or 1),
                "audit_count": audit_count,
                "generation_plan": plan,
                "audit_preview_url": (
                    f"/api/runs/{run.id}/oracle-preview?table_code={table_code}&dataset=audit&limit=100"
                    if (result.get("oracle_tables") or {}).get(table_code)
                    else None
                ),
                "golden_preview_url": (
                    f"/api/runs/{run.id}/oracle-preview?table_code={table_code}&dataset=golden&limit=100"
                    if (result.get("oracle_target_tables") or {}).get(table_code)
                    else None
                ),
            }
        )

    active_hashes = set(result.get("active_rule_hashes") or [])
    approved_hashes = {row.rule_hash for row in approved}
    summary = {key: value for key, value in report.items() if key != "cases"} or (result.get("audit_summary") or {})
    return {
        "run_id": run.id,
        "status": state.status,
        "ai_generated": bool(generators),
        "audit_stale": bool(active_hashes and active_hashes != approved_hashes),
        "summary": summary,
        "rules": rules,
        "cases": cases,
        "test_data": test_data,
        "coverage_statement": report.get("coverage_statement")
        or "结果仅对本次透明展示的 AI 审计参数负责，不代表监管语义完备。",
    }
