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

from app.infra.asset_models import (
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


def _issue(severity: str, code: str, message: str, field_en: str = "", **extra: Any) -> Dict[str, Any]:
    return {"severity": severity, "code": code, "message": message, "field_en": field_en, **extra}


def evaluate_table(
    session: Session,
    table: TableMetadataModel,
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """评估一张表的交付物：能建表 / 能灌码值 / 规则原料是否齐备。"""
    if context is None:
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
        code_sets = (
            {
                row.id: row
                for row in session.query(CodeSetModel)
                .filter(CodeSetModel.id.in_({ref.code_set_id for ref in refs}))
                .all()
            }
            if refs
            else {}
        )
    else:
        fields = context["fields_by_table"].get(table.id, [])
        refs_by_field = context["refs_by_field"]
        refs = [ref for field in fields for ref in refs_by_field.get(field.id, [])]
        referenced_ids = {ref.code_set_id for ref in refs}
        code_sets = {code_set_id: context["code_sets"][code_set_id] for code_set_id in referenced_ids if code_set_id in context["code_sets"]}

    ddl: List[Dict[str, Any]] = []
    materials: List[Dict[str, Any]] = []
    codes: List[Dict[str, Any]] = []
    code_set_fields: Dict[str, set] = {}

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
            if ref.status == "EXTERNAL":
                # 原文引用了外部规范（见部分债权人代码表、外汇局代码表、QFII 编号等），
                # 本地没有对应码表可建——这不是缺失，不需要补任何东西。
                # 但取值约束仍留在原文里，阶段 2 的 AI 能读到。
                materials.append(
                    _issue(
                        "WARNING",
                        "EXTERNAL_CODE_REFERENCE",
                        f"码值引用「{ref.reference_text}」指向外部规范，本地没有对应码表，按原文字面约束",
                        name,
                    )
                )
                continue
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
            code_set_fields.setdefault(code_set.id, set()).add(name)

    duplicates = [name for name, count in seen.items() if count > 1]
    if duplicates:
        ddl.append(_issue("ERROR", "DUPLICATE_FIELD", f"字段名重复：{'、'.join(duplicates[:5])}"))

    # 码表质量只认 code_set_readiness 那一套判断。
    # 以前这里又写了一遍（未发布=ERROR、码值 0=ERROR…），两套逻辑必然漂移，
    # 结果就是码表页说「86 张齐备 83」，元数据页却报 110 个码值问题，数字对不上。
    from app.assets.code_sets.readiness import evaluate_code_set, is_reference_placeholder

    for code_set_id, code_set in code_sets.items():
        # 按这张表的视角检查：码值超长是相对引用字段长度说的，
        # 不带 table_id 会把别的表短字段的超长问题算到这张表头上，
        # 用户看到 ✕ 点进码表页却找不到要改什么。
        context_values = context["values_by_set"].get(code_set.id, []) if context else None
        context_refs = None
        if context:
            context_refs = [
                (ref, field)
                for ref, field in context["refs_with_field_by_set"].get(code_set.id, [])
                if field.table_id == table.id
            ]
        verdict = evaluate_code_set(
            session,
            code_set,
            table_id=table.id,
            values_override=context_values,
            refs_override=context_refs,
        )
        if is_reference_placeholder(verdict):
            # 占位不是码表，用户没法补；引用它的字段上面已经按 UNRESOLVED 报过了
            continue
        affected = "、".join(sorted(code_set_fields.get(code_set_id, []))[:3])
        for blocker in verdict["blockers"]:
            extra = {key: value for key, value in blocker.items() if key not in {"severity", "code", "message"}}
            codes.append(
                _issue(
                    "ERROR",
                    blocker["code"],
                    f"码表「{code_set.code}」{blocker['message']}" + (f"（字段 {affected}）" if affected else ""),
                    affected,
                    # 让前端能定位到具体码表（跳过去补）和具体码值（在详情里标出来）
                    code_set_id=code_set_id,
                    code_set_code=code_set.code,
                    **extra,
                )
            )
        if not verdict["value_count"]:
            materials.append(
                _issue(
                    "WARNING",
                    "EMPTY_CODE_SET",
                    f"码表「{code_set.code}」没有码值，AI 会自己编 ENUM 白名单",
                    affected,
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
        return {
            # applicable=False 表示这张表根本没有这一项交付物。
            # 报表字段和码值没有必然联系：备注、金额、日期都不该有码值引用。
            # 这种「不适用」不能被渲染成「通过」——它既不是齐也不是缺，
            # 混进统计口径会让人以为验过了。
            "applicable": applicable,
            "ready": not blockers,
            "blockers": blockers,
            # defects 就是 blockers 的全部：码表发布已经自动化，
            # 「未发布」不再是一种阻断，剩下的全是需要人补的数据问题
            "defects": blockers,
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

    def passing(item) -> bool:
        """能进阶段 2：DDL 能落地、规则原料够、码值没有需要人补的缺陷。"""
        return (
            item["ddl"]["ready"]
            and item["rule_materials"]["ready"]
            and not item["code_sets"]["defects"]
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
        # 字面意义的「三项齐」：三项都适用，且三项都真正就绪
        "ready_all_full": sum(
            1
            for item in results
            if all(item[artifact].get("applicable", True) for artifact in ARTIFACTS)
            and all(item[artifact]["ready"] for artifact in ARTIFACTS)
        ),
        "blocker_codes": {
            artifact: sorted(codes.items(), key=lambda pair: pair[1], reverse=True)
            for artifact, codes in counter.items()
        },
    }


def split_preflight_issues(readiness: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """把一张表的就绪问题分成「阶段 2 一定失败」和「只是质量下降」。

    hard：空码表、码值超长、码值引用解析不到。`load_published_code_sets` 遇到这些
    是 `raise WorkflowError`，阶段 2 直接退出——所以不能给「确认风险继续」的逃生口，
    确认了照样失败，只是让人白点一次。
    soft：没有校验原文之类。AI 只能凭字段名猜规则，但流程能跑完。
    """
    hard = list(readiness["rule_materials"]["blockers"]) + list(readiness["code_sets"]["blockers"])
    # 同一个问题会在两个维度各报一次（空码表：code_sets 是 ERROR 进不了包，
    # rule_materials 是 WARNING 拿不到白名单）。hard 既然已经拦住了，
    # soft 里再重复一遍只会让确认框出现无意义的内容。
    hard_keys = {(item.get("code"), item.get("field_en")) for item in hard}
    soft = [
        item
        for item in list(readiness["rule_materials"].get("warnings") or []) + list(readiness["code_sets"].get("warnings") or [])
        if item.get("severity") != "ERROR" and (item.get("code"), item.get("field_en")) not in hard_keys
    ]
    return {"hard": hard, "soft": soft}


def latest_table_rows(
    session: Session,
    document_id: Optional[str] = None,
    status: Optional[str] = None,
) -> List[TableMetadataModel]:
    """每张表只留最新版本行。

    同一个表重新提取会追加一条新版本行，旧行保留用于追溯。列表接口一直是这么去重的，
    但就绪度这边没有，于是出现「汇总 151 张、列表 150 张」——差的就是 SAFE_6_9_2
    的两个版本行。统计口径和列表必须看到同一批数据，否则用户会以为系统在瞎报。
    """
    query = session.query(TableMetadataModel)
    if status:
        query = query.filter(TableMetadataModel.status == status)
    if document_id:
        query = query.filter(TableMetadataModel.document_id == document_id)
    rows = query.order_by(
        TableMetadataModel.source_ordinal.asc(), TableMetadataModel.table_code.asc()
    ).all()
    latest: Dict[tuple, TableMetadataModel] = {}
    for row in rows:
        key = (row.document_id, row.table_code)
        current = latest.get(key)
        if current is None or (row.current_version or 0) >= (current.current_version or 0):
            latest[key] = row
    live_ids = {row.id for row in latest.values()}
    return [row for row in rows if row.id in live_ids]


def evaluate_document(session: Session, document_id: Optional[str] = None) -> Dict[str, Any]:
    tables = latest_table_rows(session, document_id=document_id)
    if not tables:
        return {"summary": summarize([]), "tables": []}
    table_ids = [table.id for table in tables]
    fields = (
        session.query(FieldMetadataModel)
        .filter(FieldMetadataModel.table_id.in_(table_ids))
        .order_by(FieldMetadataModel.sort_order)
        .all()
    )
    field_ids = [field.id for field in fields]
    refs = (
        session.query(FieldCodeReferenceModel)
        .filter(FieldCodeReferenceModel.field_id.in_(field_ids))
        .all()
        if field_ids
        else []
    )
    code_set_ids = {ref.code_set_id for ref in refs}
    code_sets = (
        session.query(CodeSetModel).filter(CodeSetModel.id.in_(code_set_ids)).all()
        if code_set_ids
        else []
    )
    values = (
        session.query(CodeValueModel).filter(CodeValueModel.code_set_id.in_(code_set_ids)).all()
        if code_set_ids
        else []
    )
    refs_with_field = (
        session.query(FieldCodeReferenceModel, FieldMetadataModel)
        .join(FieldMetadataModel, FieldCodeReferenceModel.field_id == FieldMetadataModel.id)
        .filter(FieldCodeReferenceModel.code_set_id.in_(code_set_ids))
        .all()
        if code_set_ids
        else []
    )
    fields_by_table: Dict[str, List[FieldMetadataModel]] = {}
    for field in fields:
        fields_by_table.setdefault(field.table_id, []).append(field)
    refs_by_field: Dict[str, List[FieldCodeReferenceModel]] = {}
    for ref in refs:
        refs_by_field.setdefault(ref.field_id, []).append(ref)
    values_by_set: Dict[str, List[CodeValueModel]] = {}
    for value in values:
        values_by_set.setdefault(value.code_set_id, []).append(value)
    refs_with_field_by_set: Dict[str, List] = {}
    for ref, field in refs_with_field:
        refs_with_field_by_set.setdefault(ref.code_set_id, []).append((ref, field))
    context = {
        "fields_by_table": fields_by_table,
        "refs_by_field": refs_by_field,
        "code_sets": {row.id: row for row in code_sets},
        "values_by_set": values_by_set,
        "refs_with_field_by_set": refs_with_field_by_set,
    }
    results = [evaluate_table(session, table, context=context) for table in tables]
    return {"summary": summarize(results), "tables": results}
