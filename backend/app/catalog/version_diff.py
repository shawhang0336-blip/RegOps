"""监管制度跨版本差异对比与影响面分析引擎（Layer 4: catalog）。

面向金融机构监管制度年度修订场景（如外管局 1.3 → 1.4）：
- 按 `table_code`（或归一化表名）对齐两份源文档的全部表资产；
- 逐字段对比数据类型、列宽、小数位、必填性、校验原文与码表引用；
- 按业务风险自动分级：`BREAKING`（破坏性变更） / `RULE_IMPACT`（规则影响） / `METADATA_ONLY`（仅描述变更） / `NONE`；
- 返回可直接投喂 Step 2 增量生成的 `affected_table_ids`，实现“只重跑受影响表”。
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.infra.db_tables import (
    CodeSetModel,
    CodeValueModel,
    FieldMetadataModel,
    SourceDocumentModel,
    SpecPackageModel,
    TableMetadataModel,
    TableRuleEvidenceModel,
)

_IMPACT_RANK = {"NONE": 0, "METADATA_ONLY": 1, "RULE_IMPACT": 2, "BREAKING": 3}


def _norm_name(text: str) -> str:
    return re.sub(r"[\s_\-：:（）()]+", "", str(text or "")).lower()


def _max_impact(a: str, b: str) -> str:
    return a if _IMPACT_RANK.get(a, 0) >= _IMPACT_RANK.get(b, 0) else b


def _field_snapshot(field: FieldMetadataModel) -> Dict[str, Any]:
    return {
        "field_en": field.field_en,
        "field_cn": field.field_cn or "",
        "data_type": (field.data_type or "").upper(),
        "length": int(field.length or 0),
        "decimal_places": int(field.decimal_places or 0),
        "required": bool(field.required),
        "check_text_raw": (field.check_text_raw or "").strip(),
        "code_reference_text": (field.code_reference_text or "").strip(),
    }


def _diff_table_fields(
    base_fields: List[FieldMetadataModel],
    target_fields: List[FieldMetadataModel],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], str]:
    base_map = {f.field_en.upper(): _field_snapshot(f) for f in base_fields}
    target_map = {f.field_en.upper(): _field_snapshot(f) for f in target_fields}

    added: List[Dict[str, Any]] = []
    removed: List[Dict[str, Any]] = []
    modified: List[Dict[str, Any]] = []
    table_impact = "NONE"

    for key, snap in target_map.items():
        if key not in base_map:
            sev = "BREAKING" if snap["required"] else "RULE_IMPACT"
            added.append({**snap, "impact": sev})
            table_impact = _max_impact(table_impact, sev)

    for key, snap in base_map.items():
        if key not in target_map:
            removed.append({**snap, "impact": "BREAKING"})
            table_impact = _max_impact(table_impact, "BREAKING")

    tracked_attrs = (
        "data_type",
        "length",
        "decimal_places",
        "required",
        "check_text_raw",
        "code_reference_text",
        "field_cn",
    )
    for key in sorted(set(base_map) & set(target_map)):
        old = base_map[key]
        new = target_map[key]
        changes: List[Dict[str, Any]] = []
        field_impact = "NONE"
        for attr in tracked_attrs:
            if old[attr] == new[attr]:
                continue
            if attr == "data_type":
                sev = "BREAKING"
            elif attr == "length":
                sev = "BREAKING" if new["length"] < old["length"] else "RULE_IMPACT"
            elif attr == "decimal_places":
                sev = "BREAKING" if new["decimal_places"] < old["decimal_places"] else "RULE_IMPACT"
            elif attr == "required":
                sev = "BREAKING" if (not old["required"] and new["required"]) else "RULE_IMPACT"
            elif attr in {"check_text_raw", "code_reference_text"}:
                sev = "RULE_IMPACT"
            else:
                sev = "METADATA_ONLY"
            field_impact = _max_impact(field_impact, sev)
            changes.append({"attr": attr, "old": old[attr], "new": new[attr], "impact": sev})
        if changes:
            modified.append(
                {
                    "field_en": new["field_en"],
                    "field_cn": new["field_cn"],
                    "impact": field_impact,
                    "changes": changes,
                }
            )
            table_impact = _max_impact(table_impact, field_impact)

    added.sort(key=lambda x: x["field_en"])
    removed.sort(key=lambda x: x["field_en"])
    modified.sort(key=lambda x: (-_IMPACT_RANK[x["impact"]], x["field_en"]))
    return added, removed, modified, table_impact


def _load_tables_and_fields(
    session: Session,
    *,
    document_id: Optional[str] = None,
    package_id: Optional[str] = None,
) -> Tuple[List[TableMetadataModel], Dict[str, List[FieldMetadataModel]]]:
    """只加载每张表的最新版本有效行（过滤 MERGED 子表与历史旧版本），并按 500 项分片防 Oracle ORA-01795。"""
    from app.catalog.tables.readiness import latest_table_rows

    tables = latest_table_rows(session, document_id=document_id, package_id=package_id)
    table_ids = [t.id for t in tables]
    fields_by_table: Dict[str, List[FieldMetadataModel]] = {tid: [] for tid in table_ids}
    for idx in range(0, len(table_ids), 500):
        chunk = table_ids[idx : idx + 500]
        for field in (
            session.query(FieldMetadataModel)
            .filter(FieldMetadataModel.table_id.in_(chunk))
            .order_by(FieldMetadataModel.sort_order, FieldMetadataModel.field_en)
            .all()
        ):
            fields_by_table.setdefault(field.table_id, []).append(field)
    return tables, fields_by_table


def _compare_table_collections(
    base_tables: List[TableMetadataModel],
    base_fields: Dict[str, List[FieldMetadataModel]],
    target_tables: List[TableMetadataModel],
    target_fields: Dict[str, List[FieldMetadataModel]],
) -> Tuple[Dict[str, Any], List[str], List[Dict[str, Any]]]:
    base_by_code = {t.table_code.upper(): t for t in base_tables}
    base_by_name = {_norm_name(t.table_name): t for t in base_tables if _norm_name(t.table_name)}

    matched_base_ids = set()
    table_diffs: List[Dict[str, Any]] = []
    affected_table_ids: List[str] = []

    for tgt in target_tables:
        base = base_by_code.get(tgt.table_code.upper())
        if base is None:
            candidate = base_by_name.get(_norm_name(tgt.table_name))
            if candidate is not None and candidate.id not in matched_base_ids:
                base = candidate
        if base is None:
            t_fields = [_field_snapshot(f) for f in target_fields.get(tgt.id, [])]
            table_diffs.append(
                {
                    "change_kind": "ADDED",
                    "impact": "RULE_IMPACT",
                    "table_code": tgt.table_code,
                    "table_name": tgt.table_name,
                    "base_table_id": None,
                    "target_table_id": tgt.id,
                    "target_status": tgt.status,
                    "added_fields": t_fields,
                    "removed_fields": [],
                    "modified_fields": [],
                    "field_counts": {"base": 0, "target": len(t_fields)},
                }
            )
            affected_table_ids.append(tgt.id)
            continue

        matched_base_ids.add(base.id)
        added_f, removed_f, modified_f, impact = _diff_table_fields(
            base_fields.get(base.id, []),
            target_fields.get(tgt.id, []),
        )
        changed = bool(added_f or removed_f or modified_f or base.table_code != tgt.table_code)
        if base.table_code != tgt.table_code:
            impact = _max_impact(impact, "BREAKING")
        change_kind = "MODIFIED" if changed else "UNCHANGED"
        if impact in {"BREAKING", "RULE_IMPACT"}:
            affected_table_ids.append(tgt.id)
        table_diffs.append(
            {
                "change_kind": change_kind,
                "impact": impact,
                "table_code": tgt.table_code,
                "base_table_code": base.table_code if base.table_code != tgt.table_code else None,
                "table_name": tgt.table_name,
                "base_table_id": base.id,
                "target_table_id": tgt.id,
                "target_status": tgt.status,
                "added_fields": added_f,
                "removed_fields": removed_f,
                "modified_fields": modified_f,
                "field_counts": {
                    "base": len(base_fields.get(base.id, [])),
                    "target": len(target_fields.get(tgt.id, [])),
                },
            }
        )

    for base in base_tables:
        if base.id in matched_base_ids:
            continue
        b_fields = [_field_snapshot(f) for f in base_fields.get(base.id, [])]
        table_diffs.append(
            {
                "change_kind": "REMOVED",
                "impact": "BREAKING",
                "table_code": base.table_code,
                "table_name": base.table_name,
                "base_table_id": base.id,
                "target_table_id": None,
                "target_status": None,
                "added_fields": [],
                "removed_fields": b_fields,
                "modified_fields": [],
                "field_counts": {"base": len(b_fields), "target": 0},
            }
        )

    table_diffs.sort(
        key=lambda item: (
            -_IMPACT_RANK.get(item["impact"], 0),
            item["change_kind"] == "UNCHANGED",
            item["table_code"],
        )
    )
    summary = {
        "total_base_tables": len(base_tables),
        "total_target_tables": len(target_tables),
        "added_tables": sum(1 for item in table_diffs if item["change_kind"] == "ADDED"),
        "removed_tables": sum(1 for item in table_diffs if item["change_kind"] == "REMOVED"),
        "modified_tables": sum(1 for item in table_diffs if item["change_kind"] == "MODIFIED"),
        "unchanged_tables": sum(1 for item in table_diffs if item["change_kind"] == "UNCHANGED"),
        "breaking_tables": sum(1 for item in table_diffs if item["impact"] == "BREAKING"),
        "rule_impact_tables": sum(1 for item in table_diffs if item["impact"] == "RULE_IMPACT"),
        "affected_table_count": len(affected_table_ids),
    }
    return summary, affected_table_ids, table_diffs


def diff_documents(session: Session, base_doc_id: str, target_doc_id: str) -> Dict[str, Any]:
    """对比两份监管制度文档的表结构与字段校验差异，计算影响面并生成增量重跑表清单。"""
    base_doc = session.get(SourceDocumentModel, base_doc_id)
    target_doc = session.get(SourceDocumentModel, target_doc_id)
    if not base_doc:
        raise ValueError(f"Base document not found: {base_doc_id}")
    if not target_doc:
        raise ValueError(f"Target document not found: {target_doc_id}")

    base_tables, base_fields = _load_tables_and_fields(session, document_id=base_doc_id)
    target_tables, target_fields = _load_tables_and_fields(session, document_id=target_doc_id)
    summary, affected_table_ids, table_diffs = _compare_table_collections(
        base_tables, base_fields, target_tables, target_fields
    )
    return {
        "base_document": {
            "id": base_doc.id,
            "file_name": base_doc.file_name,
            "spec_version": base_doc.spec_version,
            "created_at": base_doc.created_at.isoformat() if base_doc.created_at else None,
        },
        "target_document": {
            "id": target_doc.id,
            "file_name": target_doc.file_name,
            "spec_version": target_doc.spec_version,
            "created_at": target_doc.created_at.isoformat() if target_doc.created_at else None,
        },
        "summary": summary,
        "affected_table_ids": affected_table_ids,
        "tables": table_diffs,
    }


def _load_latest_code_sets(
    session: Session,
    package_id: str,
) -> Tuple[List[CodeSetModel], Dict[str, List[CodeValueModel]]]:
    rows = (
        session.query(CodeSetModel)
        .filter(
            CodeSetModel.package_id == package_id,
            CodeSetModel.status != "PLACEHOLDER",
        )
        .order_by(CodeSetModel.code.asc(), CodeSetModel.current_version.desc())
        .all()
    )
    latest: Dict[str, CodeSetModel] = {}
    for row in rows:
        key = (row.code or "").strip().upper()
        if key and key not in latest:
            latest[key] = row
    sets = list(latest.values())
    set_ids = [cs.id for cs in sets]
    values_by_set: Dict[str, List[CodeValueModel]] = {sid: [] for sid in set_ids}
    for idx in range(0, len(set_ids), 500):
        chunk = set_ids[idx : idx + 500]
        for val in (
            session.query(CodeValueModel)
            .filter(CodeValueModel.code_set_id.in_(chunk))
            .order_by(CodeValueModel.sort_order.asc(), CodeValueModel.value.asc())
            .all()
        ):
            values_by_set.setdefault(val.code_set_id, []).append(val)
    return sets, values_by_set


def _compare_code_sets(
    session: Session,
    base_package_id: str,
    target_package_id: str,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    base_sets, base_vals = _load_latest_code_sets(session, base_package_id)
    target_sets, target_vals = _load_latest_code_sets(session, target_package_id)

    base_by_code = {cs.code.upper(): cs for cs in base_sets}
    base_by_name = {_norm_name(cs.name): cs for cs in base_sets if _norm_name(cs.name)}
    matched_base_ids = set()
    diffs: List[Dict[str, Any]] = []
    total_added_vals = 0
    total_removed_vals = 0

    for tgt in target_sets:
        base = base_by_code.get(tgt.code.upper())
        if base is None:
            cand = base_by_name.get(_norm_name(tgt.name))
            if cand is not None and cand.id not in matched_base_ids:
                base = cand
        t_list = target_vals.get(tgt.id, [])
        if base is None:
            total_added_vals += len(t_list)
            diffs.append(
                {
                    "change_kind": "ADDED",
                    "impact": "RULE_IMPACT",
                    "code": tgt.code,
                    "name": tgt.name,
                    "value_counts": {"base": 0, "target": len(t_list)},
                    "added_values": [{"value": v.value, "label_cn": v.label_cn} for v in t_list[:80]],
                    "removed_values": [],
                    "modified_values": [],
                }
            )
            continue

        matched_base_ids.add(base.id)
        b_list = base_vals.get(base.id, [])
        b_map = {v.value: v for v in b_list}
        t_map = {v.value: v for v in t_list}

        added_v = [{"value": v.value, "label_cn": v.label_cn} for k, v in t_map.items() if k not in b_map]
        removed_v = [{"value": v.value, "label_cn": v.label_cn} for k, v in b_map.items() if k not in t_map]
        modified_v = []
        for k in sorted(set(b_map) & set(t_map)):
            bv, tv = b_map[k], t_map[k]
            if (bv.label_cn or "").strip() != (tv.label_cn or "").strip() or bool(bv.enabled) != bool(tv.enabled):
                modified_v.append(
                    {
                        "value": k,
                        "old_label_cn": bv.label_cn,
                        "new_label_cn": tv.label_cn,
                        "old_enabled": bool(bv.enabled),
                        "new_enabled": bool(tv.enabled),
                    }
                )
        total_added_vals += len(added_v)
        total_removed_vals += len(removed_v)

        if removed_v or any(m["old_enabled"] and not m["new_enabled"] for m in modified_v):
            impact = "BREAKING"
            change_kind = "MODIFIED"
        elif added_v or modified_v:
            impact = "RULE_IMPACT"
            change_kind = "MODIFIED"
        else:
            impact = "NONE"
            change_kind = "UNCHANGED"

        diffs.append(
            {
                "change_kind": change_kind,
                "impact": impact,
                "code": tgt.code,
                "name": tgt.name,
                "value_counts": {"base": len(b_list), "target": len(t_list)},
                "added_values": added_v[:80],
                "removed_values": removed_v[:80],
                "modified_values": modified_v[:80],
            }
        )

    for base in base_sets:
        if base.id in matched_base_ids:
            continue
        b_list = base_vals.get(base.id, [])
        total_removed_vals += len(b_list)
        diffs.append(
            {
                "change_kind": "REMOVED",
                "impact": "BREAKING",
                "code": base.code,
                "name": base.name,
                "value_counts": {"base": len(b_list), "target": 0},
                "added_values": [],
                "removed_values": [{"value": v.value, "label_cn": v.label_cn} for v in b_list[:80]],
                "modified_values": [],
            }
        )

    diffs.sort(key=lambda x: (-_IMPACT_RANK.get(x["impact"], 0), x["change_kind"] == "UNCHANGED", x["code"]))
    summary = {
        "base": len(base_sets),
        "target": len(target_sets),
        "added": sum(1 for d in diffs if d["change_kind"] == "ADDED"),
        "removed": sum(1 for d in diffs if d["change_kind"] == "REMOVED"),
        "modified": sum(1 for d in diffs if d["change_kind"] == "MODIFIED"),
        "unchanged": sum(1 for d in diffs if d["change_kind"] == "UNCHANGED"),
        "added_values": total_added_vals,
        "removed_values": total_removed_vals,
    }
    return summary, diffs


def _rule_evidence_view(row: TableRuleEvidenceModel) -> Dict[str, Any]:
    return {
        "id": row.id,
        "table_code": row.table_code,
        "table_name": row.table_name,
        "rule_code": row.rule_code,
        "rule_name": row.rule_name,
        "severity": row.severity,
        "scope": row.scope,
        "target_fields": list(row.target_fields_json or []),
        "rule_text": row.rule_text,
        "reference_sql": row.reference_sql or "",
        "rule_hash": row.rule_hash,
    }


def _compare_rule_evidences(
    session: Session,
    base_package_id: str,
    target_package_id: str,
) -> Tuple[Dict[str, Any], Dict[str, List[Dict[str, Any]]]]:
    base_rows = (
        session.query(TableRuleEvidenceModel)
        .filter(TableRuleEvidenceModel.package_id == base_package_id)
        .order_by(TableRuleEvidenceModel.table_code.asc(), TableRuleEvidenceModel.rule_code.asc())
        .all()
    )
    target_rows = (
        session.query(TableRuleEvidenceModel)
        .filter(TableRuleEvidenceModel.package_id == target_package_id)
        .order_by(TableRuleEvidenceModel.table_code.asc(), TableRuleEvidenceModel.rule_code.asc())
        .all()
    )

    base_by_hash = {r.rule_hash: r for r in base_rows}
    target_by_hash = {r.rule_hash: r for r in target_rows}
    unchanged_hashes = set(base_by_hash) & set(target_by_hash)

    rem_base = [r for h, r in base_by_hash.items() if h not in unchanged_hashes]
    rem_target = [r for h, r in target_by_hash.items() if h not in unchanged_hashes]

    base_by_code_key: Dict[Tuple[str, str], TableRuleEvidenceModel] = {}
    for r in rem_base:
        if r.rule_code:
            base_by_code_key[(r.table_code.upper(), r.rule_code.upper())] = r

    matched_base_hashes = set()
    added: List[Dict[str, Any]] = []
    modified: List[Dict[str, Any]] = []
    for tr in rem_target:
        key = (tr.table_code.upper(), tr.rule_code.upper()) if tr.rule_code else None
        br = base_by_code_key.get(key) if key else None
        if br is not None and br.rule_hash not in matched_base_hashes:
            matched_base_hashes.add(br.rule_hash)
            modified.append(
                {
                    "table_code": tr.table_code,
                    "table_name": tr.table_name or br.table_name,
                    "rule_code": tr.rule_code,
                    "rule_name": tr.rule_name or br.rule_name,
                    "old": _rule_evidence_view(br),
                    "new": _rule_evidence_view(tr),
                }
            )
        else:
            added.append(_rule_evidence_view(tr))

    removed = [_rule_evidence_view(br) for br in rem_base if br.rule_hash not in matched_base_hashes]

    summary = {
        "base": len(base_by_hash),
        "target": len(target_by_hash),
        "added": len(added),
        "removed": len(removed),
        "modified": len(modified),
        "unchanged": len(unchanged_hashes),
    }
    return summary, {"added": added[:200], "removed": removed[:200], "modified": modified[:200]}


def diff_packages(session: Session, base_package_id: str, target_package_id: str) -> Dict[str, Any]:
    """对比两套多文件监管规范包的表结构、字段校验、码表码值与补充核查规则差异。"""
    base_pkg = session.get(SpecPackageModel, base_package_id)
    target_pkg = session.get(SpecPackageModel, target_package_id)
    if not base_pkg:
        raise ValueError(f"Base package not found: {base_package_id}")
    if not target_pkg:
        raise ValueError(f"Target package not found: {target_package_id}")

    base_tables, base_fields = _load_tables_and_fields(session, package_id=base_package_id)
    target_tables, target_fields = _load_tables_and_fields(session, package_id=target_package_id)
    summary, affected_table_ids, table_diffs = _compare_table_collections(
        base_tables, base_fields, target_tables, target_fields
    )

    code_set_summary, code_set_diffs = _compare_code_sets(session, base_package_id, target_package_id)
    rule_summary, rule_details = _compare_rule_evidences(session, base_package_id, target_package_id)
    summary["code_sets"] = code_set_summary
    summary["rule_evidences"] = rule_summary

    return {
        "base_package": {
            "id": base_pkg.id,
            "name": base_pkg.name,
            "regulator": base_pkg.regulator,
            "spec_version": base_pkg.spec_version,
            "created_at": base_pkg.created_at.isoformat() if base_pkg.created_at else None,
        },
        "target_package": {
            "id": target_pkg.id,
            "name": target_pkg.name,
            "regulator": target_pkg.regulator,
            "spec_version": target_pkg.spec_version,
            "created_at": target_pkg.created_at.isoformat() if target_pkg.created_at else None,
        },
        "summary": summary,
        "affected_table_ids": affected_table_ids,
        "tables": table_diffs,
        "code_sets": code_set_diffs,
        "rule_evidences": rule_details,
    }
