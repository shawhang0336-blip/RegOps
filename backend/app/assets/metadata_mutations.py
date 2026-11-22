"""元数据人工编辑、发布、删除与批量类型导入（Metadata Mutations）。

职责边界：
- 单字段编辑 `update_field`（自动记录审计 Revision 并将人工修改类型标为 `MANUAL`）；
- 表级删除 `remove_table` 与版本发布 `publish_table`（联动自动发布引用码表）；
- 批量修正字段类型 `apply_field_type_import`（支持 CSV/XLSX 导入与 dry-run 预检）。
"""

import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.assets.metadata_domain import MetadataTableDraft, require_valid_metadata
from app.assets.metadata_views import (
    TYPE_BASIS_INFERRED,
    TYPE_BASIS_MANUAL,
    TYPE_BASIS_SOURCE,
    _field_view,
)
from app.infra.models import (
    FieldCodeReferenceModel,
    FieldMetadataModel,
    MetadataRevisionModel,
    MetadataValidationModel,
    TableMetadataModel,
)


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
    old = _field_view(field)
    for key, value in changes.items():
        setattr(field, key, value)
    # 人工改过类型/长度，就不再是"原文声明"或"AI 推断"，标成人工口径
    if {"data_type", "length", "decimal_places"} & set(changes) and "type_basis" not in changes:
        field.type_basis = TYPE_BASIS_MANUAL
    if "code_reference_text" in changes:
        from app.assets.code_sets.linking import ensure_field_reference

        ensure_field_reference(session, field)
    table.current_version += 1
    table.status = "DRAFT"
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
    from app.assets.code_sets.service import ensure_table_references
    from app.assets.readiness import evaluate_table

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
    from app.assets.code_sets.readiness import auto_publish_referenced_code_sets

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
    tables = {
        table.table_code.upper(): table
        for table in session.query(TableMetadataModel).all()
        if not document_id or table.document_id == document_id
    }
    versions: Dict[str, int] = {}
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

        candidates = [tables[table_code]] if table_code and table_code in tables else list(tables.values())
        field = None
        table = None
        for candidate in candidates:
            field = (
                session.query(FieldMetadataModel)
                .filter(FieldMetadataModel.table_id == candidate.id, FieldMetadataModel.field_en == field_en)
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

        changes: Dict[str, Any] = {}
        if target_type and (field.data_type or "").upper() != target_type:
            changes["data_type"] = target_type
        if target_length is not None and int(field.length or 0) != target_length:
            changes["length"] = target_length
        if target_dec is not None and int(field.decimal_places or 0) != target_dec:
            changes["decimal_places"] = target_dec
        if not changes:
            unchanged.append({"line": line_no, "table_code": table.table_code, "field_en": field_en})
            continue
        if table.status == "PUBLISHED":
            errors.append({"line": line_no, "message": f"{table.table_code}.{field_en} 已发布，不可直接修改"})
            continue
        old = {
            "data_type": field.data_type,
            "length": field.length,
            "decimal_places": field.decimal_places,
            "type_basis": field.type_basis,
        }
        for key, value in changes.items():
            setattr(field, key, value)
        field.type_basis = TYPE_BASIS_MANUAL
        if not dry_run:
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
    if dry_run:
        session.rollback()
    else:
        session.commit()
    return {
        "updated": len(updated),
        "unchanged": len(unchanged),
        "errors": errors,
        "details": updated,
        "dry_run": dry_run,
    }
