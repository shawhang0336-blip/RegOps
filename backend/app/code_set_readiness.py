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


def evaluate_code_set(
    session: Session,
    row: CodeSetModel,
    table_id: Optional[str] = None,
) -> Dict[str, Any]:
    """检查一张码表能不能用。

    table_id 只在「从某张表的角度看」时传：码值超长是相对引用字段的长度说的，
    同一个码表被 3 位长的字段和 128 位长的字段同时引用时，前者会撞 ORA-12899、
    后者完全没问题。不传就是全局视角（码表页、自动发布用），
    传了只看这张表自己的字段——否则每张引用过它的表都会背上别人的问题，
    用户点进去发现自己的字段根本不超长。
    """
    total_values = (
        session.query(CodeValueModel)
        .filter(CodeValueModel.code_set_id == row.id)
        .order_by(CodeValueModel.sort_order, CodeValueModel.value)
        .all()
    )
    total_value_count = len(total_values)
    values = [value for value in total_values if value.enabled]
    refs_query = (
        session.query(FieldCodeReferenceModel, FieldMetadataModel)
        .join(FieldMetadataModel, FieldCodeReferenceModel.field_id == FieldMetadataModel.id)
        .filter(FieldCodeReferenceModel.code_set_id == row.id)
    )
    if table_id:
        refs_query = refs_query.filter(FieldMetadataModel.table_id == table_id)
    refs = refs_query.all()
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
        "source_type": row.source_type,
        "current_version": row.current_version,
        "published_version": row.published_version,
        "value_count": len(values),
        # 全部码值数（含被禁用的）。占位判断要看这个——一条都没有才算占位，
        # 全部被禁用是「有数据但没启用」，不是占位。
        "total_value_count": total_value_count,
        "reference_count": len(refs),
        "referencing_tables": sorted({field.table_id for _, field in refs}),
        "ready": not blockers,
        "blockers": blockers,
        "warnings": warnings,
    }


def is_reference_placeholder(item: Dict[str, Any]) -> bool:
    """匹配失败留下的空壳，不是可维护的码表。

    字段说"取值见 XXX 码表"但原文里没找到那张表时，会建一条 DOCUMENT_REFERENCE
    且 0 码值的占位，好让字段的引用有地方挂。它既不该出现在码表列表里（用户没法
    补，那不是表，是没解析到），也不该算进"需补码值"——那是"引用没解析到"，
    归元数据页的 CODE_REFERENCE_UNRESOLVED 管。
    """
    if item.get("source_type") != "DOCUMENT_REFERENCE":
        return False
    total = item.get("total_value_count")
    if total is None:
        total = item.get("value_count", 0)
    return total == 0


def summarize_code_sets(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    blockers: Dict[str, int] = {}
    for item in results:
        for blocker in item["blockers"]:
            blockers[blocker["code"]] = blockers.get(blocker["code"], 0) + 1
    referenced = [item for item in results if item["reference_count"] > 0]
    ready = sum(1 for item in results if not item["blockers"])
    return {
        "total": len(results),
        # 码表「发布」已经自动化（选表生成时自动发），所以「已发布多少张」对用户没有意义。
        # 用户要看的只有两类：齐备的、以及需要回原文补的。
        "ready": ready,
        "blocked": len(results) - ready,
        "referenced": len(referenced),
        "referenced_ready": sum(1 for item in referenced if not item["blockers"]),
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
    # 和列表接口用同一套可见性规则，否则就会出现「汇总 96 张、列表 86 张」这种对不上
    results = [item for item in results if not is_reference_placeholder(item)]
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
