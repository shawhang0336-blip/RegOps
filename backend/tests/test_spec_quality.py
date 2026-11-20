from app.ingestion.spec_quality import inspect_block, inspect_spec


def _spec(fields):
    return {"table_code": "T", "fields": fields}


def test_fragment_check_text_is_flagged():
    for fragment in ("+", "和", "-", "="):
        issues = inspect_spec(_spec([{"name": "A0117", "content": "实收资本", "check_text": fragment, "data_type": "NUMBER"}]))
        codes = [item["code"] for item in issues]
        assert "FRAGMENT_CHECK_TEXT" in codes


def test_legitimate_short_check_text_is_not_flagged():
    issues = inspect_spec(_spec([{"name": "A0101", "content": "机构代码", "check_text": "必填项", "data_type": "VARCHAR"}]))
    assert "FRAGMENT_CHECK_TEXT" not in [item["code"] for item in issues]


def test_truncated_name_and_missing_type_are_flagged():
    issues = inspect_spec(_spec([{"name": "A0102", "content": "称", "check_text": "必填项", "data_type": ""}]))
    codes = [item["code"] for item in issues]
    assert "TRUNCATED_FIELD_NAME" in codes
    assert "MISSING_TYPE" in codes


def test_duplicate_and_empty_names():
    issues = inspect_spec(
        _spec(
            [
                {"name": "A1", "content": "字段一", "check_text": "必填项", "data_type": "VARCHAR"},
                {"name": "A1", "content": "字段一重复", "check_text": "必填项", "data_type": "VARCHAR"},
                {"name": "", "content": "没有名字", "check_text": "必填项", "data_type": "VARCHAR"},
            ]
        )
    )
    codes = [item["code"] for item in issues]
    assert "DUPLICATE_FIELD" in codes
    assert "EMPTY_FIELD_NAME" in codes


def test_empty_spec_is_error():
    issues = inspect_spec({"fields": []})
    assert issues[0]["code"] == "EMPTY_SPEC"
    assert issues[0]["severity"] == "ERROR"


def test_clean_spec_has_no_errors():
    issues = inspect_spec(
        _spec(
            [
                {"name": "A0101", "content": "境外被投资机构代码", "check_text": "必填项。同一报告期唯一", "data_type": "VARCHAR"},
                {"name": "A0102", "content": "境外被投资机构全称", "check_text": "必填项。", "data_type": "VARCHAR"},
            ]
        )
    )
    assert [item for item in issues if item["severity"] == "ERROR"] == []


def test_inspect_block_handles_code_set():
    assert inspect_block({"content_json": {"code_set_spec": {"values": []}}})[0]["code"] == "EMPTY_CODE_SET"
    assert inspect_block({"content_json": {"code_set_spec": {"values": [{"value": "1"}]}}}) == []
