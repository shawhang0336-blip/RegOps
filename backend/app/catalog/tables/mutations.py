"""元数据人工编辑、发布、删除与批量类型导入（Metadata Mutations）。

职责边界：
- 单字段编辑 `update_field`（自动记录审计 Revision 并将人工修改类型标为 `MANUAL`）；
- 表级删除 `remove_table` 与版本发布 `publish_table`（联动自动发布引用码表）；
- 批量修正字段类型 `apply_field_type_import`（支持 CSV/XLSX 导入与 dry-run 预检）。
"""

import re
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from app.catalog.tables.domain import MetadataTableDraft, require_valid_metadata
from app.catalog.tables.views import (
    TYPE_BASIS_INFERRED,
    TYPE_BASIS_MANUAL,
    TYPE_BASIS_SOURCE,
    _field_view,
)
from app.infra.db_tables import (
    FieldCodeReferenceModel,
    FieldMetadataModel,
    MetadataRevisionModel,
    MetadataValidationModel,
    RawBlockModel,
    TableMetadataModel,
)


def claim_table_version_bump(
    session: Session,
    table: TableMetadataModel,
    expected_version: int,
    *,
    new_status: str = "DRAFT",
) -> None:
    """原子 CAS 递增表版本，防止并发请求基于相同 expected_version 静默覆盖。"""
    matched = (
        session.query(TableMetadataModel)
        .filter(
            TableMetadataModel.id == table.id,
            TableMetadataModel.current_version == expected_version,
            TableMetadataModel.status != "MERGED",
        )
        .update(
            {
                TableMetadataModel.current_version: expected_version + 1,
                TableMetadataModel.status: new_status,
            },
            synchronize_session=False,
        )
    )
    if matched != 1:
        raise ValueError("VERSION_CONFLICT")
    set_committed_value(table, "current_version", expected_version + 1)
    set_committed_value(table, "status", new_status)


def update_field(
    session: Session,
    table: TableMetadataModel,
    field: FieldMetadataModel,
    changes: Dict[str, Any],
    expected_version: int,
    editor: str,
    reason: str,
) -> None:
    if table.current_version != expected_version:
        raise ValueError("VERSION_CONFLICT")
    # 已发布不再不可改：改完 status 变 DRAFT、版本号 +1，
    # 下次进阶段 2 会重新自动定稿。「不可变」这个要求本来就没有依据——
    # 旧版本靠 run 里的 spec 快照复现，不靠这一行冻结。
    allowed = {
        "field_cn",
        "field_en",
        "field_definition",
        "check_text_raw",
        "data_type",
        "length",
        "decimal_places",
        "required",
        "code_reference_text",
        "confidence",
        "type_basis",
    }
    if any(key not in allowed for key in changes):
        raise ValueError("INVALID_FIELD_CHANGE")
    if "type_basis" in changes and str(changes["type_basis"]).upper() not in {
        TYPE_BASIS_SOURCE,
        TYPE_BASIS_INFERRED,
        TYPE_BASIS_MANUAL,
    }:
        raise ValueError("INVALID_TYPE_BASIS")
    normalized_changes = dict(changes)
    if "data_type" in normalized_changes:
        dt = str(normalized_changes["data_type"] or "").strip().upper()
        if dt not in {"VARCHAR", "NUMBER"}:
            raise ValueError(f"INVALID_DATA_TYPE: {normalized_changes['data_type']}")
        normalized_changes["data_type"] = dt
    if "field_en" in normalized_changes:
        new_en = str(normalized_changes["field_en"] or "").strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", new_en):
            raise ValueError(f"INVALID_FIELD_NAME: {new_en}")
        dup = (
            session.query(FieldMetadataModel)
            .filter(
                FieldMetadataModel.table_id == table.id,
                FieldMetadataModel.id != field.id,
                func.upper(FieldMetadataModel.field_en) == new_en.upper(),
            )
            .first()
        )
        if dup:
            raise ValueError(f"DUPLICATE_FIELD: {new_en}")
        normalized_changes["field_en"] = new_en[:128]
    if "field_cn" in normalized_changes:
        normalized_changes["field_cn"] = str(normalized_changes["field_cn"] or "").strip()[:255]
    eff_type = str(normalized_changes.get("data_type", field.data_type) or "VARCHAR").upper()
    eff_length = int(normalized_changes.get("length", field.length) or 0)
    eff_dec = int(normalized_changes.get("decimal_places", field.decimal_places) or 0)
    if "length" in normalized_changes and eff_length <= 0:
        raise ValueError("INVALID_LENGTH")
    if "decimal_places" in normalized_changes and eff_dec < 0:
        raise ValueError("INVALID_DECIMAL_PLACES")
    if ({"data_type", "length", "decimal_places"} & set(normalized_changes)) and eff_type == "NUMBER" and eff_dec > eff_length:
        raise ValueError("INVALID_PRECISION")
    claim_table_version_bump(session, table, expected_version, new_status="DRAFT")
    old = _field_view(field)
    for key, value in normalized_changes.items():
        setattr(field, key, value)
    # 人工改过类型/长度，就不再是"原文声明"或"AI 推断"，标成人工口径
    if {"data_type", "length", "decimal_places"} & set(normalized_changes) and "type_basis" not in normalized_changes:
        field.type_basis = TYPE_BASIS_MANUAL
    if "code_reference_text" in normalized_changes:
        from app.catalog.code_sets.linking import ensure_field_reference

        ensure_field_reference(session, field)
    session.add(
        MetadataRevisionModel(
            id=str(uuid.uuid4()),
            table_id=table.id,
            field_id=field.id,
            revision_kind="HUMAN_EDIT",
            old_value_json=old,
            new_value_json=_field_view(field),
            editor=editor,
            reason=reason,
        )
    )
    session.flush()


def remove_table(session: Session, table: TableMetadataModel) -> Dict[str, Any]:
    """删掉一张报表：字段、字段上的码值引用、历史修订一并清掉。

    历史 run 不受影响——run 存的是 spec 快照（content_json），不依赖这一行。
    码表不跟着删，它可能还被别的表引用着。
    """
    field_ids = [
        row[0]
        for row in session.query(FieldMetadataModel.id).filter(FieldMetadataModel.table_id == table.id).all()
    ]
    if field_ids:
        session.query(FieldCodeReferenceModel).filter(
            FieldCodeReferenceModel.field_id.in_(field_ids)
        ).delete(synchronize_session=False)
    session.query(MetadataRevisionModel).filter(
        MetadataRevisionModel.table_id == table.id
    ).delete(synchronize_session=False)
    # 校验记录也挂在 table_id 上（带数据库外键），不删会 1451
    session.query(MetadataValidationModel).filter(
        MetadataValidationModel.table_id == table.id
    ).delete(synchronize_session=False)
    session.query(FieldMetadataModel).filter(
        FieldMetadataModel.table_id == table.id
    ).delete(synchronize_session=False)
    code = table.table_code
    table_id = table.id
    session.delete(table)
    session.flush()
    return {"deleted": table_id, "table_code": code, "field_count": len(field_ids)}


def publish_table(session: Session, table: TableMetadataModel, approver: str, expected_version: int) -> Dict[str, Any]:
    """定稿一张表；顺带把它引用、且检查通过的码表一起发布。

    返回自动发布的码表清单，调用方可以据此告诉用户「替你发了什么」。
    """
    if table.current_version != expected_version:
        raise ValueError("VERSION_CONFLICT")
    fields = session.query(FieldMetadataModel).filter(FieldMetadataModel.table_id == table.id).order_by(FieldMetadataModel.sort_order).all()
    draft = MetadataTableDraft(
        table_code=table.table_code,
        table_name=table.table_name,
        table_definition=table.table_definition,
        fields=[
            {
                key: value
                for key, value in _field_view(field).items()
                if key not in {"id", "sort_order", "ai_value_json", "type_basis"}
            }
            for field in fields
        ],
    )
    require_valid_metadata(draft)
    from app.catalog.code_sets.linking import ensure_table_references
    from app.catalog.tables.readiness import evaluate_table

    ensure_table_references(session, table)
    session.flush()

    # 表发布是阶段 2 的唯一入口，这里必须回答"能不能落成 DDL"；规则/码值的问题
    # 不阻断发布，但要写进发布快照，避免阶段 2 才炸。
    readiness = evaluate_table(session, table)
    if not readiness["ddl"]["ready"]:
        first = readiness["ddl"]["blockers"][0]
        raise ValueError(f"NOT_READY_FOR_DDL：{first['message']}")

    # 顺手把它引用的、检查通过的码表一起发布。
    # 码表「发布」原本是独立的人工动作，但没人会为了 165 张码表挨个点，
    # 不点的话阶段 2 的 load_published_code_sets 又直接抛错。检查不通过的
    # （空码表、码值超长）留给人补数据，那才是真需要动手的地方。
    from app.catalog.code_sets.readiness import auto_publish_referenced_code_sets

    auto = auto_publish_referenced_code_sets(session, table)
    if auto["published"]:
        readiness = evaluate_table(session, table)

    table.status = "PUBLISHED"
    table.published_version = table.current_version
    session.add(
        MetadataRevisionModel(
            id=str(uuid.uuid4()),
            table_id=table.id,
            field_id=None,
            revision_kind="PUBLISHED",
            old_value_json=None,
            new_value_json={
                "version": table.current_version,
                "readiness": {
                    "rule_materials_ready": readiness["rule_materials"]["ready"],
                    "code_sets_ready": readiness["code_sets"]["ready"],
                    "rule_material_blockers": [item["code"] for item in readiness["rule_materials"]["blockers"]],
                    "code_set_blockers": [item["code"] for item in readiness["code_sets"]["blockers"]],
                },
                "auto_published_code_sets": [item["code"] for item in auto["published"]],
                "code_sets_needing_work": auto["skipped"],
            },
            editor=approver,
            reason="Human published immutable metadata",
        )
    )
    session.flush()
    return auto


# ---------------------------------------------------------------------------
# 批量修正字段类型
#
# 用户拿到的规范里，类型常常整表缺失（例如统计制度只有"数值型"没给长度），
# 逐个字段点开改不现实。这里用一张表（CSV/XLSX）一次性给出口径，
# 导入的行一律标成 MANUAL——手工给的口径不该再显示成"原文声明"或"推断"。
# ---------------------------------------------------------------------------

TYPE_ALIASES = {
    "varchar": "VARCHAR",
    "字符型": "VARCHAR",
    "字符": "VARCHAR",
    "字符串": "VARCHAR",
    "文本": "VARCHAR",
    "string": "VARCHAR",
    "char": "VARCHAR",
    "number": "NUMBER",
    "numeric": "NUMBER",
    "decimal": "NUMBER",
    "数值型": "NUMBER",
    "数值": "NUMBER",
    "数字": "NUMBER",
    "整数型": "NUMBER",
}

IMPORT_HEADER_ALIASES = {
    "table_code": "table_code",
    "报表代码": "table_code",
    "表代码": "table_code",
    "表号": "table_code",
    "field_en": "field_en",
    "字段英文名": "field_en",
    "字段": "field_en",
    "英文名": "field_en",
    "字段名": "field_en",
    "data_type": "data_type",
    "目标类型": "data_type",
    "类型": "data_type",
    "数据类型": "data_type",
    "length": "length",
    "目标长度": "length",
    "长度": "length",
    "decimal_places": "decimal_places",
    "目标小数位": "decimal_places",
    "小数位": "decimal_places",
    "reason": "reason",
    "说明": "reason",
    "备注": "reason",
}


def normalize_type_name(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    return TYPE_ALIASES.get(raw.lower()) or TYPE_ALIASES.get(raw) or ""


def apply_field_type_import(
    session: Session,
    rows: List[List[str]],
    *,
    editor: str,
    reason: str = "批量导入类型修正",
    document_id: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """按表里的行修正字段类型，返回改动明细与未导入行。

    表头通过别名识别（报表代码/字段英文名/目标类型/目标长度/目标小数位/说明），
    只写需要改的列即可；找不到字段或类型写错的行整体跳过并报行号，不中断整批。
    """
    if not rows:
        raise ValueError("文件没有内容")
    header = [str(cell or "").strip() for cell in rows[0]]
    indexes = {IMPORT_HEADER_ALIASES[name]: index for index, name in enumerate(header) if name in IMPORT_HEADER_ALIASES}
    if "field_en" not in indexes:
        raise ValueError("缺少必需列：字段英文名（field_en）")
    if not {"data_type", "length", "decimal_places"} & set(indexes):
        raise ValueError("至少要有一列要改的内容：目标类型/长度/小数位")

    def cell(values: List[str], key: str) -> str:
        index = indexes.get(key)
        if index is None or index >= len(values):
            return ""
        return str(values[index] or "").strip()

    updated: List[Dict[str, Any]] = []
    unchanged: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    from app.catalog.tables.readiness import latest_table_rows

    tables = {
        table.table_code.upper(): table
        for table in latest_table_rows(session, document_id=document_id)
    }
    versions: Dict[str, int] = {}
    dry_run_state: Dict[str, Dict[str, Any]] = {}
    for line_no, values in enumerate(rows[1:], start=2):
        if not any(str(item or "").strip() for item in values):
            continue
        field_en = cell(values, "field_en").upper()
        table_code = cell(values, "table_code").upper()
        if not field_en:
            errors.append({"line": line_no, "message": "缺少字段英文名"})
            continue
        raw_type = cell(values, "data_type")
        target_type = normalize_type_name(raw_type)
        if raw_type and not target_type:
            errors.append({"line": line_no, "message": f"无法识别的类型：{raw_type}"})
            continue
        try:
            target_length = int(cell(values, "length")) if cell(values, "length") else None
            target_dec = int(cell(values, "decimal_places")) if cell(values, "decimal_places") else None
        except ValueError:
            errors.append({"line": line_no, "message": "长度/小数位必须是整数"})
            continue
        if target_length is not None and target_length <= 0:
            errors.append({"line": line_no, "message": f"长度必须大于 0：{target_length}"})
            continue
        if target_dec is not None and target_dec < 0:
            errors.append({"line": line_no, "message": f"小数位不能为负数：{target_dec}"})
            continue

        # 显式写了报表代码但找不到时直接报错跳过，严禁静默回退去篡改其他无关报表！
        if table_code:
            if table_code not in tables:
                errors.append({"line": line_no, "message": f"找不到报表 {table_code}"})
                continue
            candidates = [tables[table_code]]
        else:
            candidates = list(tables.values())
        field = None
        table = None
        for candidate in candidates:
            field = (
                session.query(FieldMetadataModel)
                .filter(
                    FieldMetadataModel.table_id == candidate.id,
                    func.upper(FieldMetadataModel.field_en) == field_en,
                )
                .first()
            )
            if field:
                table = candidate
                break
        if not field or not table:
            errors.append(
                {
                    "line": line_no,
                    "message": f"找不到字段 {field_en}" + (f"（报表 {table_code}）" if table_code else ""),
                }
            )
            continue

        cur_state = dry_run_state.get(
            field.id,
            {
                "data_type": (field.data_type or "").upper(),
                "length": int(field.length or 0),
                "decimal_places": int(field.decimal_places or 0),
                "type_basis": field.type_basis,
            },
        )
        eff_type = target_type or cur_state["data_type"]
        eff_len = target_length if target_length is not None else cur_state["length"]
        eff_dec = target_dec if target_dec is not None else cur_state["decimal_places"]
        if eff_type == "NUMBER" and eff_dec >= eff_len:
            errors.append({"line": line_no, "message": f"NUMBER({eff_len},{eff_dec}) 小数位必须小于总长度"})
            continue

        changes: Dict[str, Any] = {}
        if target_type and cur_state["data_type"] != target_type:
            changes["data_type"] = target_type
        if target_length is not None and cur_state["length"] != target_length:
            changes["length"] = target_length
        if target_dec is not None and cur_state["decimal_places"] != target_dec:
            changes["decimal_places"] = target_dec
        if not changes:
            unchanged.append({"line": line_no, "table_code": table.table_code, "field_en": field_en})
            continue
        if table.status == "PUBLISHED":
            errors.append({"line": line_no, "message": f"{table.table_code}.{field_en} 已发布，不可直接修改"})
            continue
        old = {
            "data_type": cur_state["data_type"],
            "length": cur_state["length"],
            "decimal_places": cur_state["decimal_places"],
            "type_basis": cur_state["type_basis"],
        }
        dry_run_state[field.id] = {**old, **changes, "type_basis": TYPE_BASIS_MANUAL}
        if not dry_run:
            for key, value in changes.items():
                setattr(field, key, value)
            field.type_basis = TYPE_BASIS_MANUAL
            versions.setdefault(table.id, table.current_version)
            table.current_version = versions[table.id] + 1
            versions[table.id] += 1
            table.status = "DRAFT"
            session.add(
                MetadataRevisionModel(
                    id=str(uuid.uuid4()),
                    table_id=table.id,
                    field_id=field.id,
                    revision_kind="HUMAN_EDIT",
                    old_value_json=old,
                    new_value_json={**old, **changes, "type_basis": TYPE_BASIS_MANUAL},
                    editor=editor,
                    reason=(cell(values, "reason") or reason)[:200],
                )
            )
        updated.append({"line": line_no, "table_code": table.table_code, "field_en": field_en, "changes": changes})
    if not dry_run:
        session.commit()
    return {
        "updated": len(updated),
        "unchanged": len(unchanged),
        "errors": errors,
        "details": updated,
        "dry_run": dry_run,
    }


def merge_metadata_tables(session: Session, parent_id: str, table_ids: List[str]) -> str:
    """将父块切分后各子块提取出的表元数据合并为一张主表，优先保留 DATA_DICTIONARY 字段定义。"""
    tables = session.query(TableMetadataModel).filter(TableMetadataModel.id.in_(table_ids)).all()
    if not tables:
        raise ValueError("No metadata tables to merge")
    priority = {"NARRATIVE": 0, "TERMINOLOGY": 1, "XML_FORMAT": 2, "CODE_VALUES": 2, "PRIMARY_KEY": 2, "DATA_DICTIONARY": 3}
    blocks = {
        block.id: block
        for block in session.query(RawBlockModel).filter(RawBlockModel.id.in_([table.block_id for table in tables])).all()
    }
    tables.sort(key=lambda table: priority.get(blocks[table.block_id].block_type, 0))
    canonical = tables[-1]
    # 必须在覆盖子表状态为 MERGED 之前记录任意子表是否需要人工复核
    had_needs_review = any(table.status == "NEEDS_REVIEW" for table in tables)
    merged = {}
    for table in tables:
        block = blocks[table.block_id]
        fields = session.query(FieldMetadataModel).filter(FieldMetadataModel.table_id == table.id).order_by(FieldMetadataModel.sort_order).all()
        for field in fields:
            values = {
                key: getattr(field, key)
                for key in [
                    "field_cn",
                    "field_en",
                    "field_definition",
                    "check_text_raw",
                    "data_type",
                    "length",
                    "decimal_places",
                    "required",
                    "code_reference_text",
                    "confidence",
                    "type_basis",
                    "ai_value_json",
                ]
            }
            values["evidence_json"] = {
                **field.evidence_json,
                "segments": (field.evidence_json.get("segments") or []) + [{"block_id": block.id, "segment_type": block.block_type}],
            }
            merged[field.field_en.upper()] = values

    # 先清理 canonical 旧字段挂接的外键（FieldCodeReference / MetadataRevision.field_id），防 MySQL 1451 / Oracle ORA-02292
    old_canonical_field_ids = [
        row[0]
        for row in session.query(FieldMetadataModel.id).filter(FieldMetadataModel.table_id == canonical.id).all()
    ]
    if old_canonical_field_ids:
        session.query(FieldCodeReferenceModel).filter(
            FieldCodeReferenceModel.field_id.in_(old_canonical_field_ids)
        ).delete(synchronize_session=False)
        session.query(MetadataRevisionModel).filter(
            MetadataRevisionModel.field_id.in_(old_canonical_field_ids)
        ).update({"field_id": None}, synchronize_session=False)
        session.query(FieldMetadataModel).filter(
            FieldMetadataModel.id.in_(old_canonical_field_ids)
        ).delete(synchronize_session=False)
    session.flush()
    for index, values in enumerate(merged.values()):
        session.add(FieldMetadataModel(id=str(uuid.uuid4()), table_id=canonical.id, sort_order=index, **values))
    for table in tables:
        if table.id != canonical.id:
            table.status = "MERGED"
            session.query(MetadataValidationModel).filter(MetadataValidationModel.table_id == table.id).update({"table_id": canonical.id})
            session.query(MetadataRevisionModel).filter(MetadataRevisionModel.table_id == table.id).update({"table_id": canonical.id})
    # 防止重提父块时 (parent_id, current_version) 撞 uq_table_block_version 唯一键
    existing_parent_max = (
        session.query(func.max(TableMetadataModel.current_version))
        .filter(TableMetadataModel.block_id == parent_id, TableMetadataModel.id != canonical.id)
        .scalar()
    )
    if existing_parent_max is not None and canonical.current_version <= existing_parent_max:
        canonical.current_version = int(existing_parent_max) + 1
    canonical.block_id = parent_id
    canonical.status = "NEEDS_REVIEW" if had_needs_review else "AI_SELF_VALIDATED"
    session.flush()
    return canonical.id
