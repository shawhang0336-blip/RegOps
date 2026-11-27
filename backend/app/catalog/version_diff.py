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


def diff_packages(session: Session, base_package_id: str, target_package_id: str) -> Dict[str, Any]:
    """对比两套多文件监管规范包的表结构、字段校验与补充核查规则差异。"""
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

    base_rules = {
        r.rule_hash for r in session.query(TableRuleEvidenceModel).filter(TableRuleEvidenceModel.package_id == base_package_id).all()
    }
    target_rules = {
        r.rule_hash for r in session.query(TableRuleEvidenceModel).filter(TableRuleEvidenceModel.package_id == target_package_id).all()
    }
    summary["rule_evidences"] = {
        "base": len(base_rules),
        "target": len(target_rules),
        "added": len(target_rules - base_rules),
        "removed": len(base_rules - target_rules),
        "unchanged": len(base_rules & target_rules),
    }

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
    }
