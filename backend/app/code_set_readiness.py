"""码表能不能发布的检查。

阶段 2 的 `load_published_code_sets()` 只收已发布码表，遇到未发布直接抛错——所以
"码表发布"是阶段 1 的收尾动作，也是打通阶段 2 的必经一步。但 358 张码表逐张点开看
不现实，需要一个机械判断：这张码表现在发出去，会不会把脏数据带进产出包。

检查项（阻断）：
- 没有可用码值（空占位表，本来就不该发布）
- 码值里有空含义（label_cn 为空，落库后无法解释）
- 被引用时，码值长度超过引用字段的长度（灌库会 ORA-12899）

检查项（提示，不阻断）：
- 没有任何字段引用它（发不发都不影响阶段 2，可以先不发）
- 码值有禁用项（按启用数计算，不算错）
"""

from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from .asset_models import CodeSetModel, CodeValueModel, FieldCodeReferenceModel, FieldMetadataModel


def _issue(severity: str, code: str, message: str) -> Dict[str, Any]:
    return {"severity": severity, "code": code, "message": message}


def evaluate_code_set(session: Session, row: CodeSetModel) -> Dict[str, Any]:
    values = (
        session.query(CodeValueModel)
        .filter(CodeValueModel.code_set_id == row.id, CodeValueModel.enabled.is_(True))
        .order_by(CodeValueModel.sort_order, CodeValueModel.value)
        .all()
    )
    refs = (
        session.query(FieldCodeReferenceModel, FieldMetadataModel)
        .join(FieldMetadataModel, FieldCodeReferenceModel.field_id == FieldMetadataModel.id)
        .filter(FieldCodeReferenceModel.code_set_id == row.id)
        .all()
    )
    blockers: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []

    if not values:
        blockers.append(_issue("ERROR", "EMPTY_CODE_SET", "没有可用码值，发布后也进不了产出包"))
    missing_label = [value.value for value in values if not (value.label_cn or "").strip()]
    if missing_label:
        blockers.append(
            _issue(
                "ERROR",
                "MISSING_LABEL",
                f"{len(missing_label)} 个码值没有中文含义（如 {'、'.join(missing_label[:3])}）",
            )
        )

    too_long = []
    for ref, field in refs:
        limit = int(field.length or 0)
        if not limit:
            continue
        for value in values:
            if len(str(value.value)) > limit:
                too_long.append(f"{value.value}（字段 {field.field_en} 限 {limit} 位）")
                break
    if too_long:
        blockers.append(
            _issue("ERROR", "VALUE_TOO_LONG", f"码值长度超过引用字段长度：{'；'.join(too_long[:3])}")
        )

    if not refs:
        warnings.append(_issue("WARNING", "NOT_REFERENCED", "没有字段引用它，发不发布都不影响阶段 2"))

    return {
        "code_set_id": row.id,
        "code": row.code,
        "name": row.name,
        "status": row.status,
        "current_version": row.current_version,
        "published_version": row.published_version,
        "value_count": len(values),
        "reference_count": len(refs),
        "referencing_tables": sorted({field.table_id for _, field in refs}),
        "ready": not blockers,
        "blockers": blockers,
        "warnings": warnings,
    }


def summarize_code_sets(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    blockers: Dict[str, int] = {}
    for item in results:
        for blocker in item["blockers"]:
            blockers[blocker["code"]] = blockers.get(blocker["code"], 0) + 1
    published = sum(1 for item in results if item["status"] == "PUBLISHED")
    referenced = [item for item in results if item["reference_count"] > 0]
    return {
        "total": len(results),
        "published": published,
        "referenced": len(referenced),
        "ready_to_publish": sum(1 for item in results if item["ready"] and item["status"] != "PUBLISHED"),
        "ready_and_referenced": sum(
            1 for item in referenced if item["ready"] and item["status"] != "PUBLISHED"
        ),
        "blocker_codes": sorted(blockers.items(), key=lambda pair: pair[1], reverse=True),
    }


def evaluate_code_sets(
    session: Session,
    *,
    document_id: Optional[str] = None,
    referenced_only: bool = False,
) -> Dict[str, Any]:
    query = session.query(CodeSetModel)
    if document_id:
        from .code_service import document_code_set_ids

        ids = document_code_set_ids(session, document_id)
        query = query.filter(CodeSetModel.id.in_(ids))
    rows = query.order_by(CodeSetModel.source_ordinal.asc(), CodeSetModel.code.asc()).all()
    results = [evaluate_code_set(session, row) for row in rows]
    if referenced_only:
        results = [item for item in results if item["reference_count"] > 0]
    return {"summary": summarize_code_sets(results), "code_sets": results}


def auto_publish_referenced_code_sets(session: Session, table) -> Dict[str, List[Dict[str, Any]]]:
    """把这张表引用、且检查通过的码表直接发布。

    「发布」原本是让逐张点的人工动作，但 AI 从原文抽出来的码表有几百张，
    人不会挨个看——点了也只是走个形式，不点的话阶段 2 又直接报错。
    所以把判断交给机械检查：通过就自动发布，不通过才留给人工。

    注意：发布只写 status/published_version，不动码值内容；产物包仍记录版本，
    事后能追溯用的是哪一版。
    """
    from .asset_models import CodeSetModel, FieldCodeReferenceModel, FieldMetadataModel

    published: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    field_ids = [
        row[0]
        for row in session.query(FieldMetadataModel.id).filter(FieldMetadataModel.table_id == table.id).all()
    ]
    if not field_ids:
        return {"published": published, "skipped": skipped}
    references = (
        session.query(FieldCodeReferenceModel)
        .filter(FieldCodeReferenceModel.field_id.in_(field_ids))
        .all()
    )
    seen = set()
    for reference in references:
        if not reference.code_set_id or reference.code_set_id in seen:
            continue
        seen.add(reference.code_set_id)
        row = session.get(CodeSetModel, reference.code_set_id)
        if not row:
            continue
        if row.status == "PUBLISHED" and row.published_version == row.current_version:
            continue
        result = evaluate_code_set(session, row)
        if not result["ready"]:
            blocker = result["blockers"][0]
            skipped.append({"code": row.code, "reason": blocker["code"], "message": blocker["message"]})
            continue
        row.status = "PUBLISHED"
        row.published_version = row.current_version
        published.append({"code": row.code, "value_count": result["value_count"]})
    if published or skipped:
        session.flush()
    return {"published": published, "skipped": skipped}
