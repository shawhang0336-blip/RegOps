import uuid

from app.asset_models import (
    CodeSetModel,
    CodeValueModel,
    FieldCodeReferenceModel,
    FieldMetadataModel,
    RawBlockModel,
    SourceDocumentModel,
    TableMetadataModel,
)
from app.metadata_service import publish_table
from app.readiness import evaluate_document, evaluate_table, split_preflight_issues, summarize
from .helpers import make_session


def make_table(session, table_code="T1", fields=None, code_set_status=None):
    document = SourceDocumentModel(
        id=str(uuid.uuid4()),
        file_name="sample.pdf",
        file_type="pdf",
        sha256=uuid.uuid4().hex,
        blob_uri="blob://test",
        spec_version="TEST",
        status="RAW_EXTRACTED",
        block_count=1,
    )
    block = RawBlockModel(
        id=str(uuid.uuid4()),
        document_id=document.id,
        ordinal=3,
        block_type="TABLE_METADATA",
        raw_content="",
        source_locator={},
        content_json={},
        raw_hash="abc",
    )
    table = TableMetadataModel(
        id=str(uuid.uuid4()),
        document_id=document.id,
        block_id=block.id,
        table_code=table_code,
        table_name="测试表",
        table_definition="测试",
        status="DRAFT",
        current_version=1,
        source_ordinal=3,
    )
    session.add_all([document, block, table])

    code_set = None
    if code_set_status:
        code_set = CodeSetModel(
            id=str(uuid.uuid4()),
            code="CURRENCY",
            name="币种代码表",
            definition="",
            source_type="DOCUMENT_TABLE",
            status=code_set_status,
            current_version=1,
            published_version=1 if code_set_status == "PUBLISHED" else None,
        )
        session.add(code_set)
        session.add(
            CodeValueModel(
                id=str(uuid.uuid4()),
                code_set_id=code_set.id,
                value="USD",
                label_cn="美元",
                sort_order=0,
                enabled=True,
                source_locator={},
            )
        )

    for index, item in enumerate(fields or []):
        field = FieldMetadataModel(
            id=str(uuid.uuid4()),
            table_id=table.id,
            field_cn=item.get("cn", "字段"),
            field_en=item["en"],
            field_definition="说明",
            check_text_raw=item.get("check", "必填项"),
            data_type=item.get("type", "VARCHAR"),
            length=item.get("length", 32),
            decimal_places=item.get("decimals", 0),
            required=item.get("required", False),
            code_reference_text=item.get("code_ref", ""),
            confidence=0.9,
            evidence_json={},
            sort_order=index,
            ai_value_json={},
            type_basis=item.get("basis", "SOURCE"),
        )
        session.add(field)
        if code_set and item.get("code_ref"):
            session.add(
                FieldCodeReferenceModel(
                    id=str(uuid.uuid4()),
                    field_id=field.id,
                    code_set_id=code_set.id,
                    reference_text=item["code_ref"],
                    status="RESOLVED",
                    condition_json={},
                )
            )
    session.commit()
    return table


def test_clean_table_is_ready_for_all_three():
    session = make_session()
    table = make_table(session, fields=[{"en": "CUSTCODE", "type": "VARCHAR", "length": 18}])
    result = evaluate_table(session, table)
    assert result["ddl"]["ready"] is True
    assert result["rule_materials"]["ready"] is True
    assert result["code_sets"]["ready"] is True


def test_long_identifier_blocks_ddl_only():
    session = make_session()
    table = make_table(
        session,
        fields=[{"en": "of_which_bond_maturity_early_redemption_principal_interest", "length": 22}],
    )
    result = evaluate_table(session, table)
    assert result["ddl"]["ready"] is False
    assert result["ddl"]["blockers"][0]["code"] == "IDENTIFIER_TOO_LONG"
    # 规则不受影响：字段名长不影响绑规则
    assert result["rule_materials"]["ready"] is True


def test_unpublished_code_set_blocks_code_values_but_only_warns_rule_materials():
    """码表未发布：码值进不了产出包（阻断），规则原料只是少一份白名单（提示）。

    规则是阶段 2 让 AI 依据校验原文生成的，"有没有原文可读"和"码表发没发布"
    是两件事；混在一项里会让人误以为大半张表连校验原文都没有。
    """
    session = make_session()
    table = make_table(
        session,
        fields=[{"en": "CURRENCY", "length": 3, "code_ref": "币种代码表", "required": True}],
        code_set_status="AI_SELF_VALIDATED",
    )
    result = evaluate_table(session, table)
    assert result["ddl"]["ready"] is True
    assert result["rule_materials"]["ready"] is True
    assert any(item["code"] == "CODE_SET_NOT_PUBLISHED" for item in result["rule_materials"]["warnings"])
    assert result["code_sets"]["ready"] is False
    assert result["code_sets"]["blockers"][0]["code"] == "CODE_SET_NOT_PUBLISHED"


def test_published_code_set_unblocks_rules():
    session = make_session()
    table = make_table(
        session,
        fields=[{"en": "CURRENCY", "length": 3, "code_ref": "币种代码表", "required": True}],
        code_set_status="PUBLISHED",
    )
    result = evaluate_table(session, table)
    assert result["rule_materials"]["ready"] is True
    assert result["code_sets"]["ready"] is True


def test_invalid_number_precision_blocks_ddl():
    session = make_session()
    table = make_table(session, fields=[{"en": "AMT", "type": "NUMBER", "length": 6, "decimals": 6}])
    result = evaluate_table(session, table)
    assert result["ddl"]["ready"] is False
    assert result["ddl"]["blockers"][0]["code"] == "INVALID_SCALE"


def test_summary_counts_and_blocker_ranking():
    session = make_session()
    make_table(session, table_code="OK_TABLE", fields=[{"en": "CUSTCODE", "length": 18}])
    bad = make_table(session, table_code="BAD_TABLE", fields=[{"en": "X" * 40, "length": 18}])
    summary = summarize([evaluate_table(session, bad)] + [
        evaluate_table(session, t)
        for t in session.query(TableMetadataModel).filter(TableMetadataModel.table_code == "OK_TABLE").all()
    ])
    assert summary["table_count"] == 2
    assert summary["ready"]["ddl"] == 1
    assert ("IDENTIFIER_TOO_LONG", 1) in summary["blocker_codes"]["ddl"]


def test_publish_is_gated_by_ddl_readiness():
    session = make_session()
    table = make_table(session, fields=[{"en": "X" * 40, "length": 18}])
    try:
        publish_table(session, table, "tester", 1)
    except ValueError as exc:
        assert "NOT_READY_FOR_DDL" in str(exc)
    else:
        raise AssertionError("DDL 不就绪时不应允许发布")
    assert table.status != "PUBLISHED"


def test_publish_records_readiness_and_auto_publishes_code_sets():
    """表发布时顺手把检查通过的码表一起发掉。

    码表「发布」原本是独立的人工动作，但没人会为了 165 张码表挨个点；
    不点的话阶段 2 的 load_published_code_sets 又直接抛错。
    发布快照要记下「替它发了哪些」和「哪些还需要人补」。
    """
    session = make_session()
    table = make_table(
        session,
        fields=[{"en": "CURRENCY", "length": 3, "code_ref": "币种代码表", "required": True}],
        code_set_status="AI_SELF_VALIDATED",
    )
    publish_table(session, table, "tester", 1)
    assert table.status == "PUBLISHED"
    from app.asset_models import CodeSetModel, MetadataRevisionModel

    revision = (
        session.query(MetadataRevisionModel)
        .filter(MetadataRevisionModel.table_id == table.id, MetadataRevisionModel.revision_kind == "PUBLISHED")
        .one()
    )
    assert revision.new_value_json["readiness"]["rule_materials_ready"] is True
    # 码表在发布表时被自动发了，所以码值项也是就绪的
    assert revision.new_value_json["readiness"]["code_sets_ready"] is True
    assert revision.new_value_json["auto_published_code_sets"] == ["CURRENCY"]

    code_set = session.query(CodeSetModel).filter(CodeSetModel.code == "CURRENCY").one()
    assert code_set.status == "PUBLISHED"
    assert code_set.published_version == code_set.current_version


def test_publish_records_code_sets_that_still_need_work():
    """空码表不会被自动发布，要留在快照里提醒人补数据。"""
    session = make_session()
    table = make_table(
        session,
        fields=[{"en": "CURRENCY", "length": 3, "code_ref": "币种代码表", "required": True}],
        code_set_status="AI_SELF_VALIDATED",
    )
    from app.asset_models import CodeValueModel, MetadataRevisionModel

    session.query(CodeValueModel).delete()
    session.commit()

    publish_table(session, table, "tester", 1)
    revision = (
        session.query(MetadataRevisionModel)
        .filter(MetadataRevisionModel.table_id == table.id, MetadataRevisionModel.revision_kind == "PUBLISHED")
        .one()
    )
    assert revision.new_value_json["readiness"]["code_sets_ready"] is False
    assert revision.new_value_json["auto_published_code_sets"] == []
    assert revision.new_value_json["code_sets_needing_work"][0]["reason"] == "EMPTY_CODE_SET"


def test_evaluate_document_scopes_by_document():
    session = make_session()
    make_table(session, table_code="T1", fields=[{"en": "CUSTCODE", "length": 18}])
    document_id = session.query(TableMetadataModel).first().document_id
    payload = evaluate_document(session, document_id)
    assert payload["summary"]["table_count"] == 1
    assert payload["tables"][0]["table_code"] == "T1"


def test_table_without_code_reference_marks_code_sets_not_applicable():
    """报表字段和码值没有必然联系。

    备注、金额、日期这类字段本来就不该有码值引用。这种表的「码值」项是
    「不适用」——既不是齐也不是缺。以前它被算作 ready=True，界面上打一个 ✓，
    读起来像「验过了」，于是 30 张「三项齐」里混进了 5 张根本没有码值项的表。
    """
    session = make_session()
    table = make_table(session, fields=[{"en": "REMARK", "length": 256, "check": "非必填项"}])
    result = evaluate_table(session, table)
    assert result["code_sets"]["applicable"] is False
    # 不适用不阻断推进
    assert result["code_sets"]["ready"] is True


def test_table_with_code_reference_marks_code_sets_applicable():
    session = make_session()
    table = make_table(
        session,
        fields=[{"en": "CURRENCY", "length": 3, "code_ref": "币种代码表"}],
        code_set_status="PUBLISHED",
    )
    result = evaluate_table(session, table)
    assert result["code_sets"]["applicable"] is True
    assert result["code_sets"]["ready"] is True


def test_summary_separates_not_applicable_from_ready():
    session = make_session()
    make_table(session, table_code="NOCODE", fields=[{"en": "REMARK", "length": 256}])
    make_table(
        session,
        table_code="WITHCODE",
        fields=[{"en": "CURRENCY", "length": 3, "code_ref": "币种代码表"}],
        code_set_status="PUBLISHED",
    )
    summary = summarize([evaluate_table(session, table) for table in session.query(TableMetadataModel).all()])
    assert summary["not_applicable"]["code_sets"] == 1
    assert summary["ready"]["code_sets"] == 1
    # 两张都能推进阶段 2，但只有一张是真的三项都适用且都就绪
    assert summary["ready_all"] == 2
    assert summary["ready_all_full"] == 1


def test_publish_pending_is_separated_from_real_defects():
    """「码表没发布」是动作没做，「码表是空的」是数据缺失。

    两者都是阻断，但混在同一个 ✕ 里，96 张「只差点发布」的表看起来和 22 张
    「真缺码值」一样，用户根本看不出哪张要动手补数据。
    """
    session = make_session()
    table = make_table(
        session,
        fields=[{"en": "CURRENCY", "length": 3, "code_ref": "币种代码表"}],
        code_set_status="AI_SELF_VALIDATED",
    )
    result = evaluate_table(session, table)["code_sets"]
    assert result["ready"] is False
    assert len(result["publish_pending"]) == 1
    assert result["defects"] == []


def test_summary_counts_tables_that_only_need_publishing():
    session = make_session()
    make_table(
        session,
        table_code="ONLYPENDING",
        fields=[{"en": "CURRENCY", "length": 3, "code_ref": "币种代码表"}],
        code_set_status="AI_SELF_VALIDATED",
    )
    summary = summarize([evaluate_table(session, table) for table in session.query(TableMetadataModel).all()])
    assert summary["publish_pending_only"] == 1
    # 码表待发布不再拦「可出包」——发布表的时候会自动把它发掉
    assert summary["ready_all"] == 1
    # 但字面意义的「三项都适用且都就绪」还不成立
    assert summary["ready_all_full"] == 0
    assert summary["publish_pending_tables"] == ["ONLYPENDING"]


def test_unpublished_code_set_is_a_hard_blocker_not_confirmable():
    """阶段 2 的 load_published_code_sets 遇到未发布码表是 raise。

    所以不能给它「确认风险继续」的逃生口——确认了照样 WorkflowError，
    只是让人白点一次。用户看到「确认后继续」就以为能跑，实际必然失败。
    """
    session = make_session()
    table = make_table(
        session,
        fields=[{"en": "CURRENCY", "length": 3, "code_ref": "币种代码表"}],
        code_set_status="AI_SELF_VALIDATED",
    )
    issues = split_preflight_issues(evaluate_table(session, table))
    assert [item["code"] for item in issues["hard"]] == ["CODE_SET_NOT_PUBLISHED"]
    assert issues["soft"] == []


def test_missing_check_text_is_soft_and_confirmable():
    """没有校验原文只是让 AI 猜规则，流程能跑完，属于可确认项。"""
    session = make_session()
    table = make_table(session, fields=[{"en": "REMARK", "length": 256, "check": ""}])
    issues = split_preflight_issues(evaluate_table(session, table))
    assert issues["hard"] == []
    assert [item["code"] for item in issues["soft"]] == ["NO_CHECK_TEXT"]


def test_auto_publish_takes_over_the_manual_publish_step():
    """码表「发布」不再让人逐张点：检查通过就自动发。

    几百张码表挨个确认本来就确认不过来，而没发的话阶段 2 的
    load_published_code_sets 直接抛错退出。
    """
    from app.code_set_readiness import auto_publish_referenced_code_sets

    session = make_session()
    table = make_table(
        session,
        fields=[{"en": "CURRENCY", "length": 3, "code_ref": "币种代码表"}],
        code_set_status="AI_SELF_VALIDATED",
    )
    result = auto_publish_referenced_code_sets(session, table)
    assert [item["code"] for item in result["published"]] == ["CURRENCY"]
    assert result["skipped"] == []

    # 就绪度随之通过，不再卡在「待发布」
    codes = evaluate_table(session, table)["code_sets"]
    assert codes["ready"] is True
    assert codes["publish_pending"] == []

    # 幂等：已经发布的不会再发一次
    assert auto_publish_referenced_code_sets(session, table)["published"] == []


def test_auto_publish_refuses_empty_code_set():
    """空码表不能自动发出去——那正是需要人补数据的情况。"""
    from app.asset_models import CodeValueModel
    from app.code_set_readiness import auto_publish_referenced_code_sets

    session = make_session()
    table = make_table(
        session,
        fields=[{"en": "CURRENCY", "length": 3, "code_ref": "币种代码表"}],
        code_set_status="AI_SELF_VALIDATED",
    )
    session.query(CodeValueModel).delete()
    session.commit()

    result = auto_publish_referenced_code_sets(session, table)
    assert result["published"] == []
    assert result["skipped"][0]["reason"] == "EMPTY_CODE_SET"
    # 仍然卡住，需要人来处理
    assert evaluate_table(session, table)["code_sets"]["ready"] is False
