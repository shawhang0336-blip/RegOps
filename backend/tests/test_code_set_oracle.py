import uuid

import pytest

from app.asset_models import CodeSetModel, CodeValueModel
from app.code_set_oracle import (
    compile_code_set_oracle_ddl,
    compile_code_set_oracle_merge,
    load_published_code_sets,
    stable_code_set_table,
    validate_code_set_usage,
)
from app.domain import RuleDefinition
from app.generic_domain import GeneratorBinding
from app.generic_workflow import _code_set_context
from app.metadata_service import metadata_to_spec, publish_table
from app.errors import WorkflowError
from .test_metadata_service import make_metadata
from .helpers import make_session


def make_published_code_reference(session):
    table, field = make_metadata(session)
    field.code_reference_text = "国家和地区代码表"
    code_set = CodeSetModel(
        id=str(uuid.uuid4()),
        code="COUNTRY",
        name="国家和地区代码表",
        definition="测试码表",
        source_type="DOCUMENT_TABLE",
        status="PUBLISHED",
        current_version=1,
        published_version=1,
    )
    session.add(code_set)
    session.flush()
    session.add_all(
        [
            CodeValueModel(
                id=str(uuid.uuid4()),
                code_set_id=code_set.id,
                value=value,
                label_cn=label,
                label_en="",
                definition="",
                sort_order=index,
                enabled=True,
                source_locator={},
            )
            for index, (value, label) in enumerate([("CHN", "中国"), ("USA", "美国")])
        ]
    )
    publish_table(session, table, "测试发布员", 1)
    session.commit()
    return metadata_to_spec(session, table), code_set


def enum_rule(spec):
    return RuleDefinition(
        rule_key="COUNTRY_ENUM",
        table_code=spec.table_code,
        field="CUSTNAME",
        operator="ENUM",
        params={"values": ["CHN", "USA"]},
        source="DOC_EXPLICIT",
        evidence={"section": spec.section, "page": "1", "field": "CUSTNAME", "quote": "测试"},
        rationale="测试枚举规则",
    )


def test_oracle_code_set_ddl_and_merge_are_stable_and_idempotent():
    table = stable_code_set_table("国家和地区代码表")
    assert table.startswith("REG_CS_")
    assert table == stable_code_set_table("国家和地区代码表")
    ddl = compile_code_set_oracle_ddl(table)
    assert '"CODE_VALUE" VARCHAR2(128 CHAR) PRIMARY KEY' in ddl
    code_set = {"values": [{"value": "CHN", "label_cn": "中国", "sort_order": 0, "enabled": True}]}
    sql = compile_code_set_oracle_merge(code_set, table)
    assert "MERGE INTO" in sql
    assert "WHEN MATCHED THEN UPDATE" in sql
    assert "'CHN' AS CODE_VALUE" in sql


def test_published_code_set_drives_rules_and_generators():
    session = make_session()
    spec, code_set = make_published_code_reference(session)
    code_sets, fields = load_published_code_sets(session, [spec], expected_versions={"COUNTRY": 1})
    assert [item["code"] for item in code_sets] == ["COUNTRY"]
    assert fields[(spec.table_code, "CUSTNAME")]["version"] == 1
    context = _code_set_context(fields, spec.table_code, "CUSTNAME")
    assert context["published_code_set"]["values"] == ["CHN", "USA"]
    assert context["published_code_set"]["version"] == 1
    rule = enum_rule(spec)
    generators = {spec.table_code: [GeneratorBinding(field="CUSTNAME", generator="ENUM", params={"values": ["CHN"]})]}
    validate_code_set_usage([rule], generators, fields)

    rule.params["values"] = ["INVALID"]
    with pytest.raises(WorkflowError, match="outside published code set"):
        validate_code_set_usage([rule], generators, fields)

    code_set.current_version = 2
    code_set.published_version = 2
    session.commit()
    with pytest.raises(WorkflowError, match="changed after the audited run"):
        load_published_code_sets(session, [spec], expected_versions={"COUNTRY": 1})
