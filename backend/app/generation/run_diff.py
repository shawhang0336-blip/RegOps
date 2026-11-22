"""验证批次跨版本规则与 Oracle SQL 差异对比引擎（Layer 5: generation）。

支持任选两个历史验证批次（base_run_id vs target_run_id），逐条对比：
- 新增规则（ADDED）、移除规则（REMOVED）、参数/状态/来源变更规则（MODIFIED）、未变更规则（UNCHANGED）；
- 同步编译 Oracle WHERE 条件 SQL，让业务复核员一眼看清两条规则在生产数据库上的语义变化。
"""

from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.catalog.code_sets.bindings import code_set_compile_bindings, load_published_code_sets
from app.generation.sql.code_set_sql import stable_code_set_table
from app.generation.sql.compiler import compile_where
from app.infra.contracts import RuleDefinition, SpecDocument, canonical_json
from app.infra.db_tables import RuleModel, RunModel, SpecificationModel
from app.policy.regops_plan import project


def _run_code_set_bindings(session: Session, run: RunModel) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """加载该批次规格快照对应的已发布码表编译绑定，使对比 SQL 与生产实际执行的 NOT EXISTS 保持一致。"""
    spec_row = session.get(SpecificationModel, run.spec_id)
    if not spec_row or not isinstance(spec_row.content_json, dict) or not spec_row.content_json.get("tables"):
        return {}
    try:
        specs = [SpecDocument.model_validate(item) for item in spec_row.content_json["tables"]]
        code_sets, field_code_sets = load_published_code_sets(session, specs)
        if not code_sets:
            return {}
        table_by_code = {item["code"]: stable_code_set_table(item["code"]) for item in code_sets}
        return code_set_compile_bindings(field_code_sets, table_by_code)
    except Exception:
        return {}


def _compile_rule_sql(
    definition: RuleDefinition,
    code_set_bindings: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None,
) -> Optional[str]:
    try:
        sql, _ = compile_where(
            definition,
            "oracle",
            {definition.table_code: definition.table_code},
            code_set_bindings=code_set_bindings,
        )
        return sql
    except Exception:
        return None


def _rule_entry(
    row: RuleModel,
    code_set_bindings: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    definition = RuleDefinition.model_validate(row.definition_json)
    return {
        "rule_key": definition.rule_key,
        "rule_hash": row.rule_hash,
        "table_code": definition.table_code,
        "field": definition.field,
        "operator": definition.operator,
        "params": definition.params,
        "source": row.source,
        "status": row.status,
        "rationale": definition.rationale,
        "sql": _compile_rule_sql(definition, code_set_bindings=code_set_bindings),
    }


def _match_key(entry: Dict[str, Any]) -> Tuple[str, str, str, str]:
    return (entry["table_code"], entry["field"], entry["operator"], entry["rule_key"])


def _semantic_key(entry: Dict[str, Any]) -> Tuple[str, str, str]:
    return (entry["table_code"], entry["field"], entry["operator"])


def diff_runs(session: Session, base_run_id: str, target_run_id: str) -> Dict[str, Any]:
    """对比两次验证批次的有效规则集合与编译后 Oracle SQL 差异。"""
    base_run = session.get(RunModel, base_run_id)
    target_run = session.get(RunModel, target_run_id)
    if not base_run:
        raise ValueError(f"Base run not found: {base_run_id}")
    if not target_run:
        raise ValueError(f"Target run not found: {target_run_id}")

    base_rows = (
        session.query(RuleModel)
        .filter(RuleModel.run_id == base_run.id, RuleModel.status != "SUPERSEDED")
        .all()
    )
    target_rows = (
        session.query(RuleModel)
        .filter(RuleModel.run_id == target_run.id, RuleModel.status != "SUPERSEDED")
        .all()
    )

    base_bindings = _run_code_set_bindings(session, base_run)
    target_bindings = _run_code_set_bindings(session, target_run)
    base_entries = [_rule_entry(r, code_set_bindings=base_bindings) for r in base_rows]
    target_entries = [_rule_entry(r, code_set_bindings=target_bindings) for r in target_rows]

    base_by_exact: Dict[Tuple[str, str, str, str], List[Dict[str, Any]]] = {}
    base_by_semantic: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    for e in base_entries:
        base_by_exact.setdefault(_match_key(e), []).append(e)
        base_by_semantic.setdefault(_semantic_key(e), []).append(e)

    matched_base_hashes = set()
    added: List[Dict[str, Any]] = []
    removed: List[Dict[str, Any]] = []
    modified: List[Dict[str, Any]] = []
    unchanged_count = 0

    for tgt in target_entries:
        exact_candidates = [
            c for c in base_by_exact.get(_match_key(tgt), []) if c["rule_hash"] not in matched_base_hashes
        ]
        base = next((c for c in exact_candidates if c["rule_hash"] == tgt["rule_hash"]), None)
        if base is None and exact_candidates:
            base = exact_candidates[0]
        if base is None:
            candidates = [
                c for c in base_by_semantic.get(_semantic_key(tgt), [])
                if c["rule_hash"] not in matched_base_hashes
            ]
            if len(candidates) == 1:
                base = candidates[0]
        if base is None:
            added.append(tgt)
            continue
        matched_base_hashes.add(base["rule_hash"])
        diffs = []
        if canonical_json(base["params"]) != canonical_json(tgt["params"]):
            diffs.append({"attr": "params", "old": base["params"], "new": tgt["params"]})
        if base["status"] != tgt["status"]:
            diffs.append({"attr": "status", "old": base["status"], "new": tgt["status"]})
        if base["source"] != tgt["source"]:
            diffs.append({"attr": "source", "old": base["source"], "new": tgt["source"]})
        if base["sql"] != tgt["sql"]:
            diffs.append({"attr": "sql", "old": base["sql"], "new": tgt["sql"]})
        if diffs:
            modified.append(
                {
                    "table_code": tgt["table_code"],
                    "field": tgt["field"],
                    "operator": tgt["operator"],
                    "rule_key": tgt["rule_key"],
                    "base_rule_key": base["rule_key"] if base["rule_key"] != tgt["rule_key"] else None,
                    "base": base,
                    "target": tgt,
                    "changes": diffs,
                }
            )
        else:
            unchanged_count += 1

    for base in base_entries:
        if base["rule_hash"] not in matched_base_hashes:
            removed.append(base)

    added.sort(key=lambda x: (x["table_code"], x["field"], x["rule_key"]))
    removed.sort(key=lambda x: (x["table_code"], x["field"], x["rule_key"]))
    modified.sort(key=lambda x: (x["table_code"], x["field"], x["rule_key"]))

    base_state = project(session, base_run)
    target_state = project(session, target_run)
    return {
        "base_run": {
            "id": base_run.id,
            "status": base_state.status,
            "created_at": base_run.created_at.isoformat() if base_run.created_at else None,
            "rule_count": len(base_entries),
        },
        "target_run": {
            "id": target_run.id,
            "status": target_state.status,
            "created_at": target_run.created_at.isoformat() if target_run.created_at else None,
            "rule_count": len(target_entries),
        },
        "summary": {
            "added": len(added),
            "removed": len(removed),
            "modified": len(modified),
            "unchanged": unchanged_count,
        },
        "added": added,
        "removed": removed,
        "modified": modified,
    }
