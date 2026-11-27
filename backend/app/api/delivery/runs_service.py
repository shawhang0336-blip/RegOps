"""运行批次查询与审计明细聚合服务（供 `api/delivery/runs.py` 调用）。"""

import json
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.api.common import matches_regulator_filter, run_tables_from_content, run_view
from app.delivery.workflow import _regops_delete_pre_hook
from app.infra.blob_store import LocalBlobStore
from app.infra.config import Settings
from app.infra.db_tables import (
    EventModel,
    RuleModel,
    RunModel,
    SpecPackageModel,
    SpecificationModel,
    TableMetadataModel,
)
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


def list_generation_runs(
    session: Session,
    *,
    limit: int = 50,
    package_id: Optional[str] = None,
    regulator: Optional[str] = None,
) -> List[Dict]:
    """仅列出 Step 2/3 规则验证批次（关联 SpecificationModel），支持按规范包与监管体系筛选。"""
    fetch_limit = max(limit, 200) if (package_id or regulator) else limit
    runs = (
        session.query(RunModel)
        .join(SpecificationModel, RunModel.spec_id == SpecificationModel.id)
        .order_by(RunModel.created_at.desc())
        .limit(fetch_limit)
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

    # 为缺少规范包标签的历史批次建立 table_code -> SpecPackage 回溯索引
    pkgs_by_id = {
        p.id: p
        for p in session.query(SpecPackageModel).filter(SpecPackageModel.status != "DELETED").all()
    }
    pkg_by_table_code: Dict[str, SpecPackageModel] = {}
    if pkgs_by_id:
        for t in (
            session.query(TableMetadataModel)
            .filter(TableMetadataModel.package_id.isnot(None), TableMetadataModel.status != "MERGED")
            .order_by(TableMetadataModel.current_version.desc())
            .all()
        ):
            if t.package_id in pkgs_by_id and t.table_code not in pkg_by_table_code:
                pkg_by_table_code[t.table_code] = pkgs_by_id[t.package_id]

    target_pkg = (package_id or "").strip()
    target_reg = (regulator or "").strip()
    if target_reg.isascii():
        target_reg = target_reg.upper()

    results: List[Dict] = []
    for run in runs:
        content = (spec_by_id[run.spec_id].content_json or {}) if run.spec_id in spec_by_id else {}
        tables = run_tables_from_content(content)
        pkg_ids = list(content.get("package_ids") or ([content["package_id"]] if content.get("package_id") else []))
        pkg_id = content.get("package_id")
        pkg_name = content.get("package_name")
        reg = content.get("regulator")
        if not pkg_id and tables:
            matched_pkgs = [pkg_by_table_code[t["table_code"]] for t in tables if t.get("table_code") in pkg_by_table_code]
            if matched_pkgs:
                pkg_ids = list(dict.fromkeys(p.id for p in matched_pkgs))
                pkg_id = pkg_ids[0]
                pkg_name = matched_pkgs[0].name
                reg = matched_pkgs[0].regulator

        if target_pkg and target_pkg not in pkg_ids and pkg_id != target_pkg:
            continue
        if target_reg and not matches_regulator_filter(reg, target_reg):
            continue

        results.append(
            run_view(
                session,
                run,
                include_events=False,
                run_state=states[run.id],
                prefetched_rules=rules_by_run.get(run.id, []),
                prefetched_tables=tables,
                prefetched_package={
                    "package_id": pkg_id,
                    "package_ids": pkg_ids,
                    "package_name": pkg_name,
                    "regulator": reg,
                },
            )
        )
        if len(results) >= limit:
            break
    return results


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
