"""运行批次查询与审计明细聚合服务（供 `api/delivery/runs.py` 调用）。"""

import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.api.common import matches_regulator_filter, run_tables_from_content, run_view
from app.delivery.workflow import _regops_delete_pre_hook
from app.infra.blob_store import LocalBlobStore
from app.infra.config import Settings
from app.infra.db_tables import (
    ArtifactModel,
    EventModel,
    RuleModel,
    RunModel,
    SourceDocumentModel,
    SpecPackageModel,
    SpecificationModel,
    TableMetadataModel,
)
from app.kernel.event_log import GENESIS_HASH, build_trace, compute_event_hash, fold_with
from app.policy.regops_channels import resolve_event_tables
from app.policy.regops_plan import (
    fold,
    fold_ingestion_event,
    project,
    project_many,
    resolve_run_plan,
)


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


def _verify_events_chain(run_id: str, events: List[EventModel]) -> Tuple[bool, Optional[int], List[Dict[str, Any]]]:
    """在内存对已查询的事件序列执行逐条 SHA-256 哈希链校验，返回 (chain_valid, broken_seq, verified_events)。"""
    prev_hash = GENESIS_HASH
    chain_valid = True
    broken_seq: Optional[int] = None
    verified: List[Dict[str, Any]] = []
    for ev in events:
        prev_ok = ev.prev_hash == prev_hash
        expected_hash = compute_event_hash(run_id, ev.kind, ev.node, ev.payload_json, prev_hash)
        hash_ok = ev.event_hash == expected_hash
        row_ok = prev_ok and hash_ok
        if not row_ok and chain_valid:
            chain_valid = False
            broken_seq = ev.seq
        verified.append(
            {
                "seq": ev.seq,
                "kind": ev.kind,
                "node": ev.node,
                "payload": ev.payload_json if isinstance(ev.payload_json, dict) else {},
                "prev_hash": ev.prev_hash,
                "event_hash": ev.event_hash,
                "chain_ok": row_ok,
                "expected_hash": None if row_ok else expected_hash,
                "created_at": ev.created_at.isoformat() if ev.created_at else None,
            }
        )
        prev_hash = ev.event_hash if hash_ok else expected_hash
    return chain_valid, broken_seq, verified


def _extract_budget_stats(state_result: Optional[Dict[str, Any]], events: List[EventModel]) -> Dict[str, Any]:
    res = state_result if isinstance(state_result, dict) else {}
    usage = res.get("budget_usage") or (res.get("error_details") or {}).get("budget_snapshot")
    if isinstance(usage, dict):
        return {
            "llm_calls": int(usage.get("llm_calls") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "elapsed_seconds": float(usage.get("elapsed_seconds") or 0.0),
            "total_llm_seconds": float(usage.get("total_llm_seconds") or 0.0),
        }
    # 兜底：从历史事件 payload 里提取最近一次预算统计
    for ev in reversed(events):
        p = ev.payload_json if isinstance(ev.payload_json, dict) else {}
        cand = p.get("budget_usage") or (p.get("result") or {}).get("budget_usage")
        if isinstance(cand, dict):
            return {
                "llm_calls": int(cand.get("llm_calls") or 0),
                "total_tokens": int(cand.get("total_tokens") or 0),
                "prompt_tokens": int(cand.get("prompt_tokens") or 0),
                "completion_tokens": int(cand.get("completion_tokens") or 0),
                "elapsed_seconds": float(cand.get("elapsed_seconds") or 0.0),
                "total_llm_seconds": float(cand.get("total_llm_seconds") or 0.0),
            }
    return {
        "llm_calls": 0,
        "total_tokens": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "elapsed_seconds": 0.0,
        "total_llm_seconds": 0.0,
    }


def _classify_run_kind(
    run: RunModel,
    events: List[EventModel],
    spec_by_id: Dict[str, SpecificationModel],
    doc_by_id: Dict[str, SourceDocumentModel],
) -> Tuple[str, str]:
    """区分 Step 0/1 文档解析 Run（INGESTION）与 Step 2/3 规则验证 Run（GENERATION）。"""
    first = events[0] if events else None
    first_payload = first.payload_json if (first and isinstance(first.payload_json, dict)) else {}
    if first_payload.get("run_kind") == "INGESTION":
        return "INGESTION", str(first_payload.get("phase") or "extract")
    if run.spec_id in spec_by_id:
        return "GENERATION", "generation"
    if run.spec_id in doc_by_id:
        return "INGESTION", "extract"
    return "GENERATION", "generation"


def build_audit_overview(
    session: Session,
    *,
    package_id: Optional[str] = None,
    regulator: Optional[str] = None,
    run_kind: Optional[str] = None,
    limit: int = 100,
) -> Dict[str, Any]:
    """全链路 EventLog 合规审计总览：一次性纳管 Step 0/1 文档解析与 Step 2/3 规则验证账本并实时验链。"""
    fetch_limit = max(limit, 250)
    runs = (
        session.query(RunModel)
        .order_by(RunModel.created_at.desc())
        .limit(fetch_limit)
        .all()
    )
    verified_at = datetime.now(timezone.utc).isoformat()
    if not runs:
        return {
            "summary": {
                "total_runs": 0,
                "generation_runs": 0,
                "ingestion_runs": 0,
                "chain_valid_runs": 0,
                "chain_broken_runs": 0,
                "total_events": 0,
                "total_bus_rejections": 0,
                "total_self_heals": 0,
                "total_human_confirmations": 0,
                "total_llm_calls": 0,
                "total_tokens": 0,
                "verified_at": verified_at,
            },
            "runs": [],
        }

    run_ids = [r.id for r in runs]
    spec_ids = list({r.spec_id for r in runs if r.spec_id})

    events_by_run: Dict[str, List[EventModel]] = defaultdict(list)
    for ev in (
        session.query(EventModel)
        .filter(EventModel.run_id.in_(run_ids))
        .order_by(EventModel.run_id, EventModel.seq)
        .all()
    ):
        events_by_run[ev.run_id].append(ev)

    spec_by_id = {
        row.id: row
        for row in session.query(SpecificationModel).filter(SpecificationModel.id.in_(spec_ids)).all()
    }
    doc_by_id = {
        row.id: row
        for row in session.query(SourceDocumentModel).filter(SourceDocumentModel.id.in_(spec_ids)).all()
    }
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

    bundles_by_run: Dict[str, ArtifactModel] = {}
    for art in (
        session.query(ArtifactModel)
        .filter(ArtifactModel.run_id.in_(run_ids), ArtifactModel.artifact_type == "PRODUCTION_BUNDLE")
        .order_by(ArtifactModel.version.desc())
        .all()
    ):
        bundles_by_run.setdefault(art.run_id, art)

    target_pkg = (package_id or "").strip()
    target_reg = (regulator or "").strip()
    target_kind = (run_kind or "ALL").strip().upper()

    audit_rows: List[Dict[str, Any]] = []
    totals = {
        "total_runs": 0,
        "generation_runs": 0,
        "ingestion_runs": 0,
        "chain_valid_runs": 0,
        "chain_broken_runs": 0,
        "total_events": 0,
        "total_bus_rejections": 0,
        "total_self_heals": 0,
        "total_human_confirmations": 0,
        "total_llm_calls": 0,
        "total_tokens": 0,
        "verified_at": verified_at,
    }

    for run in runs:
        evs = events_by_run.get(run.id, [])
        kind, phase = _classify_run_kind(run, evs, spec_by_id, doc_by_id)
        if target_kind not in ("", "ALL") and kind != target_kind:
            continue

        pkg_id: Optional[str] = None
        pkg_ids: List[str] = []
        pkg_name: Optional[str] = None
        reg: Optional[str] = None
        title = f"批次 {run.id[:8]}"
        tables: List[Dict[str, Any]] = []
        source_sha256: Optional[str] = None

        if kind == "INGESTION":
            state = fold_with(evs, reducer=fold_ingestion_event)
            doc = doc_by_id.get(run.spec_id)
            if doc:
                title = doc.file_name
                source_sha256 = doc.sha256
                if doc.package_id:
                    pkg_id = doc.package_id
                    pkg_ids = [doc.package_id]
                    pkg = pkgs_by_id.get(doc.package_id)
                    if pkg:
                        pkg_name = pkg.name
                        reg = pkg.regulator
            elif evs and isinstance(evs[0].payload_json, dict):
                title = str(evs[0].payload_json.get("file_name") or title)
        else:
            state = fold(evs)
            spec = spec_by_id.get(run.spec_id)
            content = (spec.content_json or {}) if spec else {}
            tables = run_tables_from_content(content)
            source_sha256 = spec.source_sha256 if spec else None
            if tables:
                first_name = tables[0].get("table_name") or tables[0].get("table_code") or title
                title = first_name if len(tables) == 1 else f"{first_name} 等 {len(tables)} 张报表"
            pkg_ids = list(content.get("package_ids") or ([content["package_id"]] if content.get("package_id") else []))
            pkg_id = content.get("package_id")
            pkg_name = content.get("package_name")
            reg = content.get("regulator")
            if not pkg_id and tables:
                matched = [pkg_by_table_code[t["table_code"]] for t in tables if t.get("table_code") in pkg_by_table_code]
                if matched:
                    pkg_ids = list(dict.fromkeys(p.id for p in matched))
                    pkg_id = pkg_ids[0]
                    pkg_name = matched[0].name
                    reg = matched[0].regulator

        if target_pkg and target_pkg not in pkg_ids and pkg_id != target_pkg:
            continue
        if target_reg and not matches_regulator_filter(reg, target_reg):
            continue

        chain_valid, broken_seq, _ = _verify_events_chain(run.id, evs)
        bus_rej = sum(1 for e in evs if e.kind == "BUS_REJECTED")
        self_heals = sum(1 for e in evs if e.kind == "GO_REDIRECT")
        retries = sum(1 for e in evs if e.kind in ("NODE_RETRY", "RUN_RESUMED"))
        human_confirms = sum(
            1
            for e in evs
            if e.kind in ("HUMAN_DECISIONS_RECORDED", "TEST_RESULTS_CONFIRMED", "RULE_PARAM_EDITED")
        )
        budget = _extract_budget_stats(state.result_json, evs)
        bundle_art = bundles_by_run.get(run.id)
        audit_summary = (state.result_json or {}).get("audit_summary") or {}

        totals["total_runs"] += 1
        if kind == "INGESTION":
            totals["ingestion_runs"] += 1
        else:
            totals["generation_runs"] += 1
        if chain_valid:
            totals["chain_valid_runs"] += 1
        else:
            totals["chain_broken_runs"] += 1
        totals["total_events"] += len(evs)
        totals["total_bus_rejections"] += bus_rej
        totals["total_self_heals"] += self_heals
        totals["total_human_confirmations"] += human_confirms
        totals["total_llm_calls"] += budget["llm_calls"]
        totals["total_tokens"] += budget["total_tokens"]

        if len(audit_rows) < limit:
            audit_rows.append(
                {
                    "run_id": run.id,
                    "run_kind": kind,
                    "phase": phase,
                    "title": title,
                    "status": state.status,
                    "current_node": state.current_node,
                    "attempt": state.attempt,
                    "package_id": pkg_id,
                    "package_name": pkg_name,
                    "regulator": reg,
                    "tables": tables,
                    "event_count": len(evs),
                    "chain_valid": chain_valid,
                    "broken_seq": broken_seq,
                    "tail_hash": evs[-1].event_hash if evs else GENESIS_HASH,
                    "source_sha256": source_sha256,
                    "bundle_sha256": bundle_art.sha256 if bundle_art else None,
                    "audit_all_passed": audit_summary.get("all_passed") if isinstance(audit_summary, dict) else None,
                    "bus_rejected_count": bus_rej,
                    "self_heal_count": self_heals,
                    "retry_count": retries,
                    "human_action_count": human_confirms,
                    "llm_calls": budget["llm_calls"],
                    "total_tokens": budget["total_tokens"],
                    "elapsed_seconds": budget["elapsed_seconds"],
                    "created_at": run.created_at.isoformat() if run.created_at else None,
                    "updated_at": run.updated_at.isoformat() if run.updated_at else None,
                }
            )

    return {"summary": totals, "runs": audit_rows}


def build_audit_run_detail(session: Session, run: RunModel) -> Dict[str, Any]:
    """单次 Run 的全链路合规审计档案（哈希链逐条验签 + Trace 瀑布 + AI 门禁/人工确权清单 + 端到端指纹）。"""
    evs = (
        session.query(EventModel)
        .filter(EventModel.run_id == run.id)
        .order_by(EventModel.seq)
        .all()
    )
    spec = session.get(SpecificationModel, run.spec_id)
    doc = session.get(SourceDocumentModel, run.spec_id)
    kind, phase = _classify_run_kind(
        run,
        evs,
        {spec.id: spec} if spec else {},
        {doc.id: doc} if doc else {},
    )

    state = fold_with(evs, reducer=fold_ingestion_event) if kind == "INGESTION" else fold(evs)
    chain_valid, broken_seq, verified_events = _verify_events_chain(run.id, evs)
    trace = build_trace(evs, run_id=run.id, plan=resolve_run_plan(session, run)).as_dict()
    budget = _extract_budget_stats(state.result_json, evs)

    bus_rejections: List[Dict[str, Any]] = []
    human_actions: List[Dict[str, Any]] = []
    self_heals: List[Dict[str, Any]] = []
    for ev in verified_events:
        ek = ev["kind"]
        p = ev["payload"]
        if ek == "BUS_REJECTED":
            bus_rejections.append(
                {
                    "seq": ev["seq"],
                    "node": ev["node"],
                    "tool": p.get("tool") or "",
                    "reason": p.get("reason") or "",
                    "created_at": ev["created_at"],
                    "event_hash": ev["event_hash"],
                }
            )
        elif ek in ("HUMAN_DECISIONS_RECORDED", "TEST_RESULTS_CONFIRMED", "RULE_PARAM_EDITED", "RUN_CANCELLED", "REAUDIT_REQUESTED"):
            human_actions.append(
                {
                    "seq": ev["seq"],
                    "kind": ek,
                    "node": ev["node"],
                    "actor": p.get("approver") or p.get("reviewer") or p.get("editor") or "operator",
                    "summary": (
                        f"审批 {len(p.get('decisions') or {})} 条规则"
                        if ek == "HUMAN_DECISIONS_RECORDED"
                        else (
                            "确认 Oracle 沙箱测试结果并放行生产导出"
                            if ek == "TEST_RESULTS_CONFIRMED"
                            else (
                                f"手工调整规则参数（{p.get('rule_key') or p.get('field') or ''}）"
                                if ek == "RULE_PARAM_EDITED"
                                else str(p.get("reason") or ek)
                            )
                        )
                    ),
                    "payload": p,
                    "created_at": ev["created_at"],
                    "event_hash": ev["event_hash"],
                }
            )
        elif ek in ("GO_REDIRECT", "NODE_RETRY", "RUN_RESUMED"):
            self_heals.append(
                {
                    "seq": ev["seq"],
                    "kind": ek,
                    "node": ev["node"],
                    "target": p.get("target") or p.get("resume_from") or "",
                    "reason": p.get("reason") or "",
                    "created_at": ev["created_at"],
                    "event_hash": ev["event_hash"],
                }
            )

    source_docs: List[Dict[str, Any]] = []
    package_info: Optional[Dict[str, Any]] = None
    tables: List[Dict[str, Any]] = []
    if kind == "INGESTION" and doc:
        source_docs.append(
            {
                "name": doc.file_name,
                "sha256": doc.sha256,
                "version": doc.spec_version,
                "doc_role": doc.doc_role,
            }
        )
        if doc.package_id:
            pkg = session.get(SpecPackageModel, doc.package_id)
            if pkg:
                package_info = {"id": pkg.id, "name": pkg.name, "regulator": pkg.regulator, "spec_version": pkg.spec_version}
    elif spec:
        content = spec.content_json or {}
        tables = run_tables_from_content(content)
        spec_list = content.get("tables") or ([content] if content else [])
        seen_src = set()
        for item in spec_list:
            s_sha = item.get("source_sha256") or spec.source_sha256
            s_name = item.get("source_name") or spec.source_name
            key = (s_name, s_sha)
            if key not in seen_src:
                seen_src.add(key)
                source_docs.append(
                    {
                        "name": s_name,
                        "sha256": s_sha,
                        "version": item.get("version") or spec.version,
                        "table_code": item.get("table_code") or spec.table_code,
                    }
                )
        if content.get("package_id"):
            pkg = session.get(SpecPackageModel, content["package_id"])
            if pkg:
                package_info = {"id": pkg.id, "name": pkg.name, "regulator": pkg.regulator, "spec_version": pkg.spec_version}

    res = state.result_json or {}
    audited_hashes = res.get("active_rule_hashes")
    tamper_status = "N/A"
    if kind == "GENERATION" and isinstance(audited_hashes, list):
        approved_rows = (
            session.query(RuleModel.rule_hash)
            .filter(RuleModel.run_id == run.id, RuleModel.status == "APPROVED")
            .all()
        )
        current_hashes = sorted(r[0] for r in approved_rows)
        tamper_status = "VERIFIED" if current_hashes == sorted(audited_hashes) else "DRIFTED"

    bundle_art = (
        session.query(ArtifactModel)
        .filter(ArtifactModel.run_id == run.id, ArtifactModel.artifact_type == "PRODUCTION_BUNDLE")
        .order_by(ArtifactModel.version.desc())
        .first()
    )

    return {
        "run_id": run.id,
        "run_kind": kind,
        "phase": phase,
        "status": state.status,
        "current_node": state.current_node,
        "attempt": state.attempt,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
        "package": package_info,
        "tables": tables,
        "budget": budget,
        "chain_valid": chain_valid,
        "broken_seq": broken_seq,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "events": verified_events,
        "trace": trace,
        "bus_rejections": bus_rejections,
        "human_actions": human_actions,
        "self_heals": self_heals,
        "fingerprints": {
            "source_documents": source_docs,
            "event_chain": {
                "genesis_hash": GENESIS_HASH,
                "first_seq": verified_events[0]["seq"] if verified_events else None,
                "last_seq": verified_events[-1]["seq"] if verified_events else None,
                "tail_hash": verified_events[-1]["event_hash"] if verified_events else GENESIS_HASH,
                "event_count": len(verified_events),
                "chain_valid": chain_valid,
                "broken_seq": broken_seq,
            },
            "audit_report_blob": res.get("audit_report_blob"),
            "active_rule_count": len(audited_hashes) if isinstance(audited_hashes, list) else None,
            "rule_tamper_status": tamper_status,
            "code_set_versions": (res.get("audit_summary") or {}).get("code_set_versions") or {},
            "production_bundle": (
                {
                    "sha256": bundle_art.sha256,
                    "blob_uri": bundle_art.blob_uri,
                    "version": bundle_art.version,
                    "created_at": bundle_art.created_at.isoformat() if bundle_art.created_at else None,
                }
                if bundle_art
                else None
            ),
        },
    }
