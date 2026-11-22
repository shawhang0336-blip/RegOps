"""列表项上的删除：删掉不要的报表 / 码表。

报表清单和码值表都是 AI 从原文抽出来的，抽错了、抽重了、不是目标表的情况很常见，
要能让用户直接剔除，而不是被一堆垃圾条目拖着。
"""

import pytest

from app.infra.models import (
    CodeSetModel,
    CodeValueModel,
    FieldCodeReferenceModel,
    FieldMetadataModel,
    TableMetadataModel,
)
from app.assets.code_sets.service import remove_code_set
from app.assets.metadata_service import remove_table
from .helpers import make_session
from .test_readiness import make_table


def test_delete_table_removes_fields_and_references():
    session = make_session()
    table = make_table(
        session,
        fields=[{"en": "CURRENCY", "length": 3, "code_ref": "币种代码表"}],
        code_set_status="PUBLISHED",
    )
    field_ids = [row[0] for row in session.query(FieldMetadataModel.id).all()]
    assert field_ids

    result = remove_table(session, table)
    assert result["field_count"] == 1
    assert session.get(TableMetadataModel, table.id) is None
    assert session.query(FieldMetadataModel).filter(FieldMetadataModel.id.in_(field_ids)).count() == 0
    assert session.query(FieldCodeReferenceModel).count() == 0
    # 码表本身不受牵连——删表不等于删它引用的码表，别的表可能还在用
    assert session.query(CodeSetModel).count() == 1


def test_delete_code_set_refuses_when_referenced():
    session = make_session()
    make_table(session, fields=[{"en": "CURRENCY", "length": 3, "code_ref": "币种代码表"}], code_set_status="PUBLISHED")
    code_set = session.query(CodeSetModel).one()

    with pytest.raises(ValueError) as excinfo:
        remove_code_set(session, code_set)
    assert "被 1 个字段引用着" in str(excinfo.value)
    # 没删掉
    assert session.get(CodeSetModel, code_set.id) is not None


def test_delete_code_set_removes_values_when_unreferenced():
    session = make_session()
    make_table(session, fields=[{"en": "REMARK", "length": 256}], code_set_status="PUBLISHED")
    code_set = session.query(CodeSetModel).one()
    assert session.query(CodeValueModel).count() == 1

    result = remove_code_set(session, code_set)

    assert result["code"] == "CURRENCY"
    assert result["value_count"] == 1
    assert session.get(CodeSetModel, code_set.id) is None
    assert session.query(CodeValueModel).count() == 0
