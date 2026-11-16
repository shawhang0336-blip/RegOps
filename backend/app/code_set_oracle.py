import hashlib
import re
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from .asset_models import (
    CodeSetModel,
    CodeValueModel,
    FieldCodeReferenceModel,
    FieldMetadataModel,
    SourceDocumentModel,
    TableMetadataModel,
)
from .compiler import quote_identifier
from .domain import RuleDefinition, SpecDocument
from .generic_domain import GeneratorBinding
from .errors import WorkflowError


def stable_code_set_table(code: str) -> str:
    normalized = re.sub(r"[^A-Z0-9_]", "_", code.upper()).strip("_") or "CODE_SET"
    digest = hashlib.sha1(code.encode("utf-8")).hexdigest()[:8].upper()
    return f"REG_CS_{normalized[:80]}_{digest}"


def sandbox_code_set_table(run_id: str, attempt: int, code: str) -> str:
    run_part = run_id.replace("-", "").upper()[:12]
    return f"DQ_{run_part}_{attempt}_{stable_code_set_table(code)}"


def compile_code_set_oracle_ddl(table: str) -> str:
    quoted = quote_identifier(table, "oracle")
    return f"""CREATE TABLE {quoted} (
  "CODE_VALUE" VARCHAR2(128 CHAR) PRIMARY KEY,
  "LABEL_CN" VARCHAR2(255 CHAR) NOT NULL,
  "LABEL_EN" VARCHAR2(255 CHAR) NULL,
  "DEFINITION" VARCHAR2(2000 CHAR) NULL,
  "SORT_ORDER" NUMBER(10,0) NOT NULL,
  "ENABLED" NUMBER(1,0) NOT NULL
)"""


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def compile_code_set_oracle_merge(code_set: Dict[str, Any], table: str) -> str:
    statements = []
    quoted = quote_identifier(table, "oracle")
    for value in code_set["values"]:
        source_values = {
            "CODE_VALUE": value["value"],
            "LABEL_CN": value["label_cn"],
            "LABEL_EN": value.get("label_en") or "",
            "DEFINITION": value.get("definition") or "",
            "SORT_ORDER": int(value.get("sort_order", 0)),
            "ENABLED": 1 if value.get("enabled", True) else 0,
        }
        select = ", ".join(f"{_sql_literal(item)} AS {name}" for name, item in source_values.items())
        columns = ", ".join(f'"{name}"' for name in source_values)
        target_values = ", ".join(f"s.{name}" for name in source_values)
        updates = ", ".join(f'd."{name}" = s.{name}' for name in source_values if name != "CODE_VALUE")
        statements.append(
            f"MERGE INTO {quoted} d USING (SELECT {select} FROM dual) s "
            f'ON (d."CODE_VALUE" = s.CODE_VALUE) '
            f"WHEN MATCHED THEN UPDATE SET {updates} "
            f"WHEN NOT MATCHED THEN INSERT ({columns}) VALUES ({target_values})"
        )
    return ";\n".join(statements)


def compile_code_set_oracle_insert(code_set: Dict[str, Any], table: str) -> str:
    """Oracle INSERT ALL：用于首次初始化，重复部署请使用 MERGE 版本。"""
    quoted = quote_identifier(table, "oracle")
    columns = '("CODE_VALUE", "LABEL_CN", "LABEL_EN", "DEFINITION", "SORT_ORDER", "ENABLED")'
    statements = []
    for value in code_set["values"]:
        items = (
            value["value"],
            value["label_cn"],
            value.get("label_en") or "",
            value.get("definition") or "",
            int(value.get("sort_order", 0)),
            1 if value.get("enabled", True) else 0,
        )
        values = ", ".join(_sql_literal(item) for item in items)
        statements.append(f"  INTO {quoted} {columns} VALUES ({values})")
    if not statements:
        return ""
    return "INSERT ALL\n" + "\n".join(statements) + "\nSELECT 1 FROM DUAL"


def load_published_code_sets(
    session: Session,
    specs: List[SpecDocument],
    expected_versions: Optional[Dict[str, int]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[Tuple[str, str], Dict[str, Any]]]:
    code_sets: Dict[str, Dict[str, Any]] = {}
    field_bindings: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for spec in specs:
        table = (
            session.query(TableMetadataModel)
            .join(SourceDocumentModel, SourceDocumentModel.id == TableMetadataModel.document_id)
            .filter(
                TableMetadataModel.table_code == spec.table_code,
                TableMetadataModel.status == "PUBLISHED",
                TableMetadataModel.published_version == TableMetadataModel.current_version,
                SourceDocumentModel.sha256 == spec.source_sha256,
            )
            .order_by(TableMetadataModel.updated_at.desc())
            .first()
        )
        if not table:
            continue
        fields = session.query(FieldMetadataModel).filter(FieldMetadataModel.table_id == table.id).all()
        for field in fields:
            references = session.query(FieldCodeReferenceModel).filter(FieldCodeReferenceModel.field_id == field.id).all()
            for reference in references:
                row = session.get(CodeSetModel, reference.code_set_id)
                if not row or row.status != "PUBLISHED" or row.published_version != row.current_version:
                    raise WorkflowError(f"Code set for {spec.table_code}.{field.field_en} is not published")
                if expected_versions and row.code in expected_versions and expected_versions[row.code] != row.current_version:
                    raise WorkflowError(f"Code set {row.code} changed after the audited run; rerun the Oracle audit")
                if row.code not in code_sets:
                    values = (
                        session.query(CodeValueModel)
                        .filter(CodeValueModel.code_set_id == row.id, CodeValueModel.enabled.is_(True))
                        .order_by(CodeValueModel.sort_order, CodeValueModel.value)
                        .all()
                    )
                    if not values:
                        raise WorkflowError(f"Published code set {row.code} has no enabled values")
                    code_sets[row.code] = {
                        "id": row.id,
                        "code": row.code,
                        "name": row.name,
                        "version": row.current_version,
                        "values": [
                            {
                                "value": value.value,
                                "label_cn": value.label_cn,
                                "label_en": value.label_en,
                                "definition": value.definition,
                                "sort_order": value.sort_order,
                                "enabled": value.enabled,
                            }
                            for value in values
                        ],
                    }
                field_bindings[(spec.table_code, field.field_en)] = code_sets[row.code]
    return list(code_sets.values()), field_bindings


def code_set_compile_bindings(
    field_bindings: Dict[Tuple[str, str], Dict[str, Any]],
    table_by_code: Dict[str, str],
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """把 (表, 字段) → 已发布码表 的绑定编译成 compiler 可用的值域映射。

    只有拿到物理表名的码表才会进入映射；ENUM 规则取值与启用码值一致时，
    compiler 会改用 NOT EXISTS 子查询而不是展开 IN 列表。
    """
    bindings: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for key, binding in field_bindings.items():
        table = table_by_code.get(binding["code"])
        if table:
            bindings[key] = {"table": table, "values": [item["value"] for item in binding["values"]]}
    return bindings


def validate_code_set_usage(
    rules: List[RuleDefinition],
    golden_generators: Dict[str, List[GeneratorBinding]],
    field_bindings: Dict[Tuple[str, str], Dict[str, Any]],
) -> None:
    for rule in rules:
        if rule.operator != "ENUM":
            continue
        binding = field_bindings.get((rule.table_code, rule.field))
        if not binding:
            continue
        allowed = {item["value"] for item in binding["values"]}
        invalid = sorted(set(rule.params["values"]) - allowed)
        if invalid:
            raise WorkflowError(f"Rule {rule.rule_key} uses values outside published code set {binding['code']}: {invalid}")
    for table_code, generators in golden_generators.items():
        for generator in generators:
            if generator.generator != "ENUM":
                continue
            binding = field_bindings.get((table_code, generator.field))
            if not binding:
                continue
            allowed = {item["value"] for item in binding["values"]}
            invalid = sorted(set(generator.params.get("values", [])) - allowed)
            if invalid:
                raise WorkflowError(f"Generator {table_code}.{generator.field} uses values outside published code set {binding['code']}: {invalid}")
