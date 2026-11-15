"""阶段 1 → 阶段 2 的交付物检查。

两个阶段的分工要先说清楚，否则检查项会定错：

- 阶段 1 只负责"备料"：把**字段元数据**和**码表**准备到可用、可追溯。
- 阶段 2 才生产三样东西：建表脚本（DDL）、把码值灌进 Oracle、让 AI 依据校验原文生成
  校验规则，最后在空库上空跑验证。

所以阶段 1 这边能检查的只有三件事：

1. `ddl`：字段能不能落成 Oracle 表——标识符合法且不超过 30 字符、类型长度可映射、
   字段不重名。不满足的话阶段 2 第一步建表就 ORA-00972。
2. `code_sets`：码表能不能灌进 Oracle——必须已发布（产出包只收已发布码表）、有可用
   码值、码值长度不超过字段长度。
3. `rule_materials`：AI 生成规则时手里有没有料——校验原文在不在、取值域（码值白名单）
   拿不拿得到。这里没有"规则是否就绪"这回事，规则是阶段 2 的产物；阶段 1 只能保证
   AI 不会因为缺料而编造规则。

顺带记一个踩过的坑：不能用"校验原文里有'必填'但 required=false"来判断必填口径错了——
规范里大量字段是**条件必填**（"当对方部门为非金融企业时，此项必填"），这种字段
required 本来就该是 false，条件写在规则里。同理，从校验原文里抽取
"（ACTIONTYPE）"这类括号标识符当作字段引用也是错的，里面大量是码值（JCB、IOS、YYMMDD）。
"""

import re
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from .asset_models import (
    CodeSetModel,
    CodeValueModel,
    FieldCodeReferenceModel,
    FieldMetadataModel,
    TableMetadataModel,
)

# Oracle 11g 标识符上限；12.2 起支持 128，但生产库版本不可控，按 30 收敛
ORACLE_IDENTIFIER_MAX = 30
# Oracle VARCHAR2 在 SQL 里的上限
ORACLE_VARCHAR_MAX = 4000
ORACLE_NUMBER_PRECISION_MAX = 38
IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

ARTIFACTS = ("ddl", "rule_materials", "code_sets")


def _issue(severity: str, code: str, message: str, field_en: str = "") -> Dict[str, Any]:
    return {"severity": severity, "code": code, "message": message, "field_en": field_en}


def evaluate_table(session: Session, table: TableMetadataModel) -> Dict[str, Any]:
    """评估一张表的交付物：能建表 / 能灌码值 / 规则原料是否齐备。"""
    fields = (
        session.query(FieldMetadataModel)
        .filter(FieldMetadataModel.table_id == table.id)
        .order_by(FieldMetadataModel.sort_order)
        .all()
    )
    refs = (
        session.query(FieldCodeReferenceModel)
        .filter(FieldCodeReferenceModel.field_id.in_([field.id for field in fields]))
        .all()
        if fields
        else []
    )
    refs_by_field: Dict[str, List[FieldCodeReferenceModel]] = {}
    for ref in refs:
        refs_by_field.setdefault(ref.field_id, []).append(ref)
    code_sets: Dict[str, CodeSetModel] = {}
    if refs:
        code_sets = {
            row.id: row
            for row in session.query(CodeSetModel)
            .filter(CodeSetModel.id.in_({ref.code_set_id for ref in refs}))
            .all()
        }

    ddl: List[Dict[str, Any]] = []
    materials: List[Dict[str, Any]] = []
    codes: List[Dict[str, Any]] = []

    if not fields:
        ddl.append(_issue("ERROR", "NO_FIELDS", "这张表没有字段，无法建表"))

    seen: Dict[str, int] = {}
    for field in fields:
        name = (field.field_en or "").strip()
        if not IDENTIFIER_RE.match(name):
            ddl.append(_issue("ERROR", "INVALID_IDENTIFIER", f"字段名 {name!r} 不是合法 SQL 标识符", name))
        elif len(name) > ORACLE_IDENTIFIER_MAX:
            ddl.append(
                _issue(
                    "ERROR",
                    "IDENTIFIER_TOO_LONG",
                    f"字段名 {name} 长度 {len(name)}，超过 Oracle 标识符上限 {ORACLE_IDENTIFIER_MAX}",
                    name,
                )
            )
        seen[name] = seen.get(name, 0) + 1

        data_type = (field.data_type or "").upper()
        length = int(field.length or 0)
        decimals = int(field.decimal_places or 0)
        if data_type not in {"VARCHAR", "NUMBER"}:
            ddl.append(_issue("ERROR", "UNSUPPORTED_TYPE", f"数据类型 {data_type or '空'} 无法映射到 Oracle", name))
        elif length <= 0:
            ddl.append(_issue("ERROR", "INVALID_LENGTH", "字段长度为空或 0", name))
        elif data_type == "NUMBER":
            if length > ORACLE_NUMBER_PRECISION_MAX:
                ddl.append(
                    _issue("ERROR", "NUMBER_TOO_LARGE", f"NUMBER({length}) 超过精度上限 {ORACLE_NUMBER_PRECISION_MAX}", name)
                )
            if decimals >= length:
                ddl.append(_issue("ERROR", "INVALID_SCALE", f"NUMBER({length},{decimals}) 小数位不小于总长度", name))
        elif length > ORACLE_VARCHAR_MAX:
            ddl.append(_issue("ERROR", "VARCHAR_TOO_LARGE", f"VARCHAR({length}) 超过 Oracle 上限 {ORACLE_VARCHAR_MAX}", name))

        if not (field.check_text_raw or "").strip():
            materials.append(
                _issue("WARNING", "NO_CHECK_TEXT", "没有校验原文，阶段 2 的 AI 只能凭字段名猜规则", name)
            )

        for ref in refs_by_field.get(field.id, []):
            code_set = code_sets.get(ref.code_set_id)
            severity = "ERROR" if field.required else "WARNING"
            if ref.status == "UNRESOLVED" or not code_set:
                materials.append(
                    _issue(
                        severity,
                        "CODE_REFERENCE_UNRESOLVED",
                        f"码值引用「{ref.reference_text}」没解析到码表，AI 生成 ENUM 时拿不到取值域",
                        name,
                    )
                )
                codes.append(
                    _issue(severity, "CODE_REFERENCE_UNRESOLVED", f"码值引用「{ref.reference_text}」没解析到码表", name)
                )
                continue
            if code_set.status != "PUBLISHED" or code_set.published_version != code_set.current_version:
                # 码表没发布影响的是"码值能不能进包"，不是"AI 有没有原文可读"，
                # 所以这里只提示：ENUM 白名单拿不到，规则质量会掉。
                materials.append(
                    _issue(
                        "WARNING",
                        "CODE_SET_NOT_PUBLISHED",
                        f"引用的码表「{code_set.code}」未发布，AI 生成 ENUM 时拿不到白名单，可能编码值",
                        name,
                    )
                )
                codes.append(
                    _issue(
                        "ERROR",
                        "CODE_SET_NOT_PUBLISHED",
                        f"码表「{code_set.code}」未发布，码值不会进入产出包",
                        name,
                    )
                )

    duplicates = [name for name, count in seen.items() if count > 1]
    if duplicates:
        ddl.append(_issue("ERROR", "DUPLICATE_FIELD", f"字段名重复：{'、'.join(duplicates[:5])}"))

    for code_set_id, code_set in code_sets.items():
        value_count = (
            session.query(CodeValueModel)
            .filter(CodeValueModel.code_set_id == code_set_id, CodeValueModel.enabled.is_(True))
            .count()
        )
        if value_count == 0:
            codes.append(_issue("ERROR", "EMPTY_CODE_SET", f"码表「{code_set.code}」没有可用码值，灌不进 Oracle"))
            materials.append(
                _issue(
                    "WARNING",
                    "EMPTY_CODE_SET",
                    f"码表「{code_set.code}」没有码值，AI 会自己编 ENUM 白名单",
                )
            )

    inferred = sum(1 for field in fields if (field.type_basis or "").upper() == "INFERRED")
    ddl_warnings = [item for item in ddl if item["severity"] != "ERROR"]
    if inferred:
        ddl_warnings.append(
            _issue("WARNING", "TYPE_INFERRED", f"{inferred} 个字段的类型在原文里没有依据，是推断出来的")
        )

    def pack(items: List[Dict[str, Any]], warnings: List[Dict[str, Any]], applicable: bool = True) -> Dict[str, Any]:
        blockers = [item for item in items if item["severity"] == "ERROR"]
        # 「码表没发布」和「码表是空的」都是阻断，但性质完全不同：
        # 前者是动作没做（点一下批量发布就解决），后者是数据缺失（要人去补码值）。
        # 混在同一个 ✕ 里，96 张「只差点发布」的表看起来和 22 张「真缺码值」一样，
        # 用户根本看不出哪张要动手补数据。
        publish_pending = [item for item in blockers if item["code"] == "CODE_SET_NOT_PUBLISHED"]
        defects = [item for item in blockers if item["code"] != "CODE_SET_NOT_PUBLISHED"]
        return {
            # applicable=False 表示这张表根本没有这一项交付物。
            # 报表字段和码值没有必然联系：备注、金额、日期都不该有码值引用。
            # 这种「不适用」不能被渲染成「通过」——它既不是齐也不是缺，
            # 混进统计口径会让人以为验过了。
            "applicable": applicable,
            "ready": not blockers,
            "blockers": blockers,
            # 只差发布：数据本身是好的
            "publish_pending": publish_pending,
            # 需要补数据：空码表、引用解析不到
            "defects": defects,
            "warnings": warnings,
        }

    return {
        "table_id": table.id,
        "table_code": table.table_code,
        "table_name": table.table_name,
        "status": table.status,
        "current_version": table.current_version,
        "field_count": len(fields),
        "inferred_field_count": inferred,
        "fields_without_check_text": sum(1 for field in fields if not (field.check_text_raw or "").strip()),
        "check_text_coverage": (
            round(100 * sum(1 for field in fields if (field.check_text_raw or "").strip()) / len(fields), 1)
            if fields
            else 0.0
        ),
        "ddl": pack(ddl, ddl_warnings),
        "rule_materials": pack(materials, [item for item in materials if item["severity"] != "ERROR"]),
        # 只有引用了码表的表才有「码值」这项交付物
        "code_sets": pack(
            codes,
            [item for item in codes if item["severity"] != "ERROR"],
            applicable=bool(refs),
        ),
    }


def summarize(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """汇总：每项交付物各有多少张表可用，以及卡点排行。

    口径要说清楚：「不适用」不等于「就绪」。
    例如一张没有码值引用的报表，code_sets 这一项对它不成立，
    它不该被算进「码值就绪」，也不该阻止它推进阶段 2。
    """
    total_fields = sum(item["field_count"] for item in results)
    missing_check = sum(item["fields_without_check_text"] for item in results)
    counter: Dict[str, Dict[str, int]] = {artifact: {} for artifact in ARTIFACTS}
    ready = {artifact: 0 for artifact in ARTIFACTS}
    not_applicable = {artifact: 0 for artifact in ARTIFACTS}
    publish_pending_only: List[str] = []
    for item in results:
        for artifact in ARTIFACTS:
            section = item[artifact]
            if not section.get("applicable", True):
                not_applicable[artifact] += 1
                continue
            if section["ready"]:
                ready[artifact] += 1
            for blocker in section["blockers"]:
                counter[artifact][blocker["code"]] = counter[artifact].get(blocker["code"], 0) + 1
        # 只差「把码表点发布」就能出包的表：其它都齐，没有需要补数据的缺陷
        if (
            item["ddl"]["ready"]
            and item["rule_materials"]["ready"]
            and not item["code_sets"]["defects"]
            and item["code_sets"]["publish_pending"]
        ):
            publish_pending_only.append(item["table_code"])

    def passing(item) -> bool:
        """所有适用的项都就绪；不适用的项不参与判定。"""
        return all(
            item[artifact]["ready"] if item[artifact].get("applicable", True) else True
            for artifact in ARTIFACTS
        )

    return {
        "table_count": len(results),
        "field_count": total_fields,
        "fields_without_check_text": missing_check,
        "check_text_coverage": round(100 * (total_fields - missing_check) / total_fields, 1) if total_fields else 0.0,
        "ready": ready,
        "not_applicable": not_applicable,
        # 可推进阶段 2 的表数（含「某项不适用」的表）
        "ready_all": sum(1 for item in results if passing(item)),
        # 三项都实际适用、且都就绪的表数——这才是字面意义的「三项齐」
        "ready_all_full": sum(
            1
            for item in results
            if passing(item) and all(item[artifact].get("applicable", True) for artifact in ARTIFACTS)
        ),
        # 只差发布码表就能出包的表 —— 数据本身没问题，缺的是一个动作
        "publish_pending_only": len(publish_pending_only),
        "publish_pending_tables": publish_pending_only[:50],
        "blocker_codes": {
            artifact: sorted(codes.items(), key=lambda pair: pair[1], reverse=True)
            for artifact, codes in counter.items()
        },
    }


def evaluate_document(session: Session, document_id: Optional[str] = None) -> Dict[str, Any]:
    query = session.query(TableMetadataModel)
    if document_id:
        query = query.filter(TableMetadataModel.document_id == document_id)
    tables = query.order_by(
        TableMetadataModel.source_ordinal.asc(), TableMetadataModel.table_code.asc()
    ).all()
    results = [evaluate_table(session, table) for table in tables]
    return {"summary": summarize(results), "tables": results}
