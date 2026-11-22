import uuid

from app.infra.models import (
    CodeSetModel,
    CodeValueModel,
    FieldCodeReferenceModel,
    FieldMetadataModel,
    TableMetadataModel,
)
from app.assets.code_sets.readiness import evaluate_code_set, evaluate_code_sets, summarize_code_sets
from .helpers import make_session


def make_set(session, *, values=None, status="AI_SELF_VALIDATED", field_length=8):
    code_set = CodeSetModel(
        id=str(uuid.uuid4()),
        code="CURRENCY",
        name="币种代码表",
        definition="",
        source_type="DOCUMENT_TABLE",
        status=status,
        current_version=1,
        published_version=1 if status == "PUBLISHED" else None,
    )
    session.add(code_set)
    for index, (value, label) in enumerate(values or []):
        session.add(
            CodeValueModel(
                id=str(uuid.uuid4()),
                code_set_id=code_set.id,
                value=value,
                label_cn=label,
                sort_order=index,
                enabled=True,
                source_locator={},
            )
        )
    table = TableMetadataModel(
        id=str(uuid.uuid4()),
        document_id="doc",
        block_id="block",
        table_code="T1",
        table_name="测试表",
        table_definition="",
        status="DRAFT",
        current_version=1,
    )
    field = FieldMetadataModel(
        id=str(uuid.uuid4()),
        table_id=table.id,
        field_cn="币种",
        field_en="CURRENCY",
        field_definition="",
        check_text_raw="必填项",
        data_type="VARCHAR",
        length=field_length,
        decimal_places=0,
        required=True,
        code_reference_text="币种代码表",
        confidence=0.9,
        evidence_json={},
        sort_order=0,
        ai_value_json={},
        type_basis="SOURCE",
    )
    ref = FieldCodeReferenceModel(
        id=str(uuid.uuid4()),
        field_id=field.id,
        code_set_id=code_set.id,
        reference_text="币种代码表",
        status="RESOLVED",
        condition_json={},
        confidence=1,
    )
    session.add_all([table, field, ref])
    session.commit()
    return code_set


def test_set_with_values_is_ready():
    session = make_session()
    row = make_set(session, values=[("USD", "美元"), ("CNY", "人民币")])
    result = evaluate_code_set(session, row)
    assert result["ready"] is True
    assert result["reference_count"] == 1
    assert result["value_count"] == 2


def test_empty_set_blocks_publish():
    session = make_session()
    row = make_set(session, values=[])
    result = evaluate_code_set(session, row)
    assert result["ready"] is False
    assert result["blockers"][0]["code"] == "EMPTY_CODE_SET"


def test_missing_label_blocks_publish():
    session = make_session()
    row = make_set(session, values=[("USD", "美元"), ("CNY", "  ")])
    result = evaluate_code_set(session, row)
    assert result["ready"] is False
    assert result["blockers"][0]["code"] == "MISSING_LABEL"


def test_value_longer_than_referencing_field_blocks_publish():
    session = make_session()
    row = make_set(session, values=[("ABCDEFGHIJ", "很长")], field_length=3)
    result = evaluate_code_set(session, row)
    assert result["ready"] is False
    assert result["blockers"][0]["code"] == "VALUE_TOO_LONG"


def test_unreferenced_set_only_warns():
    session = make_session()
    row = make_set(session, values=[("USD", "美元")])
    session.query(FieldCodeReferenceModel).delete()
    session.commit()
    result = evaluate_code_set(session, row)
    assert result["ready"] is True
    assert result["reference_count"] == 0
    assert result["warnings"][0]["code"] == "NOT_REFERENCED"


def test_summary_counts_ready_and_referenced():
    session = make_session()
    make_set(session, values=[("USD", "美元")])
    payload = evaluate_code_sets(session, referenced_only=True)
    summary = payload["summary"]
    assert summary["total"] == 1
    assert summary["referenced"] == 1
    assert summary["ready"] == 1
    assert summary["blocked"] == 0
    assert summarize_code_sets([])["total"] == 0


def test_summary_does_not_report_publish_state():
    """码表发布已经自动化，「已发布多少张」对用户没有意义。

    用户只需要知道：哪些齐备、哪些要回原文补。留着「已发布/待发布」只会让人
    以为还要去哪点一下。
    """
    session = make_session()
    make_set(session, values=[("USD", "美元")])
    summary = evaluate_code_sets(session)["summary"]
    assert "published" not in summary
    assert "ready_to_publish" not in summary
    assert "ready_and_referenced" not in summary
    assert set(summary) >= {"total", "ready", "blocked", "referenced", "referenced_ready", "blocker_codes"}
