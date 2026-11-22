"""已发布码表加载、值域编译绑定与规则/造数用量校验（Catalog Code Sets，100% 数据库方言无关）。"""

from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.infra.contracts import GeneratorBinding, RuleDefinition, SpecDocument
from app.infra.db_tables import (
    CodeSetModel,
    CodeValueModel,
    FieldCodeReferenceModel,
    FieldMetadataModel,
    SourceDocumentModel,
    TableMetadataModel,
)
from app.infra.errors import WorkflowError


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
