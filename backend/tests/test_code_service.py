import uuid
from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.infra.db_tables import (
    CodeSetModel,
    CodeSetValidationModel,
    CodeValueModel,
    FieldCodeReferenceModel,
    RawBlockModel,
    SourceDocumentModel,
    TableMetadataModel,
)
from app.infra.db import Base
from app.catalog.code_sets.service import (
    CodeSetDraft,
    _upsert_code_set_draft,
    auto_link_all_references,
    extract_code_sets_for_block,
    generation_blockers,
    normalize_code_set_name,
    parse_inline_enum,
    refresh_reference_status,
)
from app.catalog.extraction import extract_parallel_for_blocks
from app.infra.config import Settings
from app.ingestion.parsers.extractors import extract_xlsx
from app.catalog.tables.service import publish_table
from .test_metadata_service import make_metadata
from .helpers import make_session


def test_unresolved_reference_blocks_then_value_unlocks():
    session = make_session()
    table, field = make_metadata(session)
    field.code_reference_text = "国别代码表"
    session.commit()
    publish_table(session, table, "发布员", 1)
    session.commit()
    blockers = generation_blockers(session, table)
    assert len(blockers) == 1
    reference = session.query(FieldCodeReferenceModel).one()
    session.add(
        CodeValueModel(
            id=str(uuid.uuid4()),
            code_set_id=reference.code_set_id,
            value="CHN",
            label_cn="中国",
            sort_order=0,
            enabled=True,
            source_locator={},
        )
    )
    refresh_reference_status(session, reference.code_set_id)
    session.commit()
    assert generation_blockers(session, table) == []


def test_normalize_code_set_name_matches_regulatory_synonyms():
    assert normalize_code_set_name("见国家和地区代码表") == normalize_code_set_name("附录（二）国家和地区代码表")
    assert normalize_code_set_name("见行业属性代码表") == normalize_code_set_name("附录（一）行业类别")
    assert normalize_code_set_name("见中国大陆行政区划代码表") == normalize_code_set_name("行政区划代码表")


def test_parallel_extraction_extracts_metadata_and_code_sets_and_autolinks(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'parallel.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr("app.catalog.extraction.SessionLocal", session_factory)
    monkeypatch.setattr("app.catalog.code_sets.service.SessionLocal", session_factory)
    monkeypatch.setattr("app.catalog.tables.service.SessionLocal", session_factory)
    session = session_factory()

    # Create a multi-sheet XLSX with one field table sheet and one code set sheet
    wb = Workbook()
    ws_meta = wb.active
    ws_meta.title = "字段字典"
    ws_meta.append(["字段中文名", "字段名", "字段校验"])
    ws_meta.append(["常驻国家代码", "COUNTRYCODE", "必填项，见国家和地区代码表"])

    ws_code = wb.create_sheet("国家和地区代码表")
    ws_code.append(["国家/地区代码", "中文简称", "英文简称"])
    ws_code.append(["CHN", "中国", "CHINA"])
    ws_code.append(["USA", "美国", "UNITED STATES"])

    xlsx_path = tmp_path / "bundle.xlsx"
    wb.save(xlsx_path)

    _, extracted_blocks = extract_xlsx(xlsx_path)
    assert len(extracted_blocks) == 2
    assert extracted_blocks[1]["content_json"]["candidate_role"] == "CODE_SET"
    assert extracted_blocks[1]["content_json"]["code_set_spec"]["code"].startswith("CS_")
    assert extracted_blocks[1]["content_json"]["code_set_spec"]["code"] != "IMPORTED_CODESET"

    doc = SourceDocumentModel(
        id=str(uuid.uuid4()),
        file_name="bundle.xlsx",
        file_type="xlsx",
        sha256=uuid.uuid4().hex,
        blob_uri="blob://bundle",
        spec_version="TEST",
        status="STRUCTURE_REVIEWED",
        block_count=len(extracted_blocks),
    )
    session.add(doc)
    block_ids = []
    for b in extracted_blocks:
        bid = str(uuid.uuid4())
        block_ids.append(bid)
        session.add(RawBlockModel(id=bid, document_id=doc.id, **b))
    session.commit()
    session.close()

    result = extract_parallel_for_blocks(block_ids, Settings(), targets=["metadata", "code_sets"])
    session = session_factory()
    assert len(result["table_ids"]) == 1
    assert len(result["code_set_ids"]) == 1
    assert result["resolved_reference_count"] == 1
    assert session.query(TableMetadataModel).count() == 1
    assert session.query(CodeSetModel).count() == 1
    assert session.query(CodeValueModel).count() == 2


def test_code_set_reimport_is_idempotent():
    session = make_session()
    draft = CodeSetDraft.model_validate(
        {
            "code": "COUNTRY",
            "name": "国家和地区",
            "values": [{"value": "CHN", "label_cn": "中国"}],
        }
    )
    row, _ = _upsert_code_set_draft(session, draft, {"page": "1"})
    session.commit()
    assert row.current_version == 1

    same_row, changed = _upsert_code_set_draft(session, draft, {"page": "1"})
    session.commit()
    assert same_row.id == row.id
    assert changed is False
    assert same_row.current_version == 1
    assert session.query(CodeValueModel).count() == 1


def test_substring_code_set_name_does_not_auto_bind():
    session = make_session()
    table, field = make_metadata(session)
    field.code_reference_text = "国家"
    existing = CodeSetModel(
        id=str(uuid.uuid4()),
        code="COUNTRY",
        name="国家和地区",
        definition="",
        source_type="DOCUMENT_TABLE",
        status="AI_SELF_VALIDATED",
        current_version=1,
    )
    session.add(existing)
    session.commit()

    assert auto_link_all_references(session) == 0
    reference = session.query(FieldCodeReferenceModel).one()
    assert reference.status == "UNRESOLVED"
    assert reference.code_set_id != existing.id


def test_code_set_ai_review_runs_once_for_unchanged_content(monkeypatch):
    session = make_session()
    monkeypatch.setattr("app.catalog.code_sets.service.SessionLocal", lambda: session)

    class FakeReviewLlm:
        calls = 0

        def assign(self, tool_name, instruction, payload, timeout=90):
            assert tool_name == "review_code_set"
            self.calls += 1
            return {"issues": []}, {"provider": "FIXTURE", "tool_name": tool_name}

    reviewer = FakeReviewLlm()
    monkeypatch.setattr("app.catalog.code_sets.service.RealDesignLlm", lambda settings: reviewer)
    document = SourceDocumentModel(
        id=str(uuid.uuid4()),
        file_name="codes.xlsx",
        file_type="xlsx",
        sha256=uuid.uuid4().hex,
        blob_uri="blob://codes",
        spec_version="TEST",
        status="STRUCTURE_REVIEWED",
        block_count=1,
    )
    block = RawBlockModel(
        id=str(uuid.uuid4()),
        document_id=document.id,
        ordinal=0,
        block_type="CODE_SET",
        raw_content="CHN 中国",
        source_locator={"sheet": "国家"},
        content_json={
            "candidate_role": "CODE_SET",
            "code_set_spec": {
                "code": "COUNTRY",
                "name": "国家和地区",
                "values": [{"value": "CHN", "label_cn": "中国"}],
            },
        },
        raw_hash="codes",
    )
    session.add_all([document, block])
    session.commit()

    extract_code_sets_for_block(block.id, Settings())
    extract_code_sets_for_block(block.id, Settings())
    row = session.query(CodeSetModel).one()
    assert row.status == "AI_SELF_VALIDATED"
    assert row.current_version == 1
    assert reviewer.calls == 1
    assert session.query(CodeSetValidationModel).filter(CodeSetValidationModel.validator_kind == "AI_REVIEW").count() == 1


def test_parse_inline_enum_tolerates_default_value_notes():
    # 原文写成 "Y-是；N-否；默认N"，尾部的默认值说明不应让整段枚举解析失败
    assert parse_inline_enum("Y-是；N-否；默认N") == [
        {"value": "Y", "label_cn": "是"},
        {"value": "N", "label_cn": "否"},
    ]
    assert [item["value"] for item in parse_inline_enum("是-Y，否-N，默认为否，填写N")] == ["Y", "N"]
    # 圆圈序号 + 说明前缀
    values = parse_inline_enum("按两类码值填列：①一年及以下；②一年以上（含永续债）")
    assert [item["value"] for item in values] == ["1", "2"]
    # 顿号分隔、码值紧贴中文
    assert [item["value"] for item in parse_inline_enum("001支付货款、002支付工程款")] == ["001", "002"]
    # AI 给出的 ENUM 数组写法
    assert len(parse_inline_enum('ENUM：["上市普通股","非上市普通股（但可交易）"]（依据原文）')) == 2


def test_parse_inline_enum_rejects_prose():
    # 只有码表名称、没有码值对的引用不能被当枚举
    assert parse_inline_enum("参照数据采集规范最新版本中的相关代码表或本制度附录（三）币种代码表填写。") == []
    assert parse_inline_enum("境内主体类型代码表/境外主体类型代码表") == []
    assert parse_inline_enum("国家/地区代码；本表限填中国（不含中国香港）") == []


def test_reference_variant_binds_real_code_set_over_placeholder():
    session = make_session()
    table, field = make_metadata(session)
    real = CodeSetModel(
        id=str(uuid.uuid4()),
        code="COUNTRY",
        name="国家和地区代码表",
        definition="",
        source_type="DOCUMENT_TABLE",
        status="AI_SELF_VALIDATED",
        current_version=1,
    )
    session.add(real)
    session.flush()
    session.add(
        CodeValueModel(
            id=str(uuid.uuid4()),
            code_set_id=real.id,
            value="CHN",
            label_cn="中国",
            sort_order=0,
            enabled=True,
            source_locator={},
        )
    )
    # "多码表并列 + 括号提示"的引用要能落到真实码表上，而不是停在自建占位表
    field.code_reference_text = "参照数据采集规范最新版本中的相关代码表（国家/地区代码）"
    session.commit()

    auto_link_all_references(session)
    session.commit()

    reference = session.query(FieldCodeReferenceModel).one()
    assert reference.code_set_id == real.id
    assert reference.status == "RESOLVED"


def test_inline_enum_fills_existing_placeholder_instead_of_creating_new_set():
    session = make_session()
    table, field = make_metadata(session)
    text = "投资工具类型代码：1-股票；2-债券；3-其他"
    field.code_reference_text = text
    # 模拟早期版本留下的占位表：表名就是引用原文，一条码值都没有
    placeholder = CodeSetModel(
        id=str(uuid.uuid4()),
        code="CUSTNAME_CODESET",
        name=text,
        definition="由字段 CUSTNAME 的校验引用自动建立",
        source_type="DOCUMENT_REFERENCE",
        status="DRAFT",
        current_version=1,
    )
    session.add(placeholder)
    session.commit()

    auto_link_all_references(session)
    session.commit()
    reference = session.query(FieldCodeReferenceModel).one()
    assert reference.code_set_id == placeholder.id
    assert reference.status == "RESOLVED"
    assert session.query(CodeSetModel).count() == 1
    assert session.query(CodeValueModel).count() == 3
    assert placeholder.source_type == "INLINE_ENUM"
    assert placeholder.name == "投资工具类型代码"

    # 再跑一次要幂等：不新建表、不重复写码值
    auto_link_all_references(session)
    session.commit()
    reference = session.query(FieldCodeReferenceModel).one()
    assert reference.code_set_id == placeholder.id
    assert session.query(CodeSetModel).count() == 1
    assert session.query(CodeValueModel).count() == 3


def test_inline_enum_without_placeholder_creates_field_scoped_set():
    session = make_session()
    table, field = make_metadata(session)
    field.code_reference_text = "交易方式枚举：1-线下；2-线上"
    session.commit()

    auto_link_all_references(session)
    session.commit()
    reference = session.query(FieldCodeReferenceModel).one()
    assert reference.status == "RESOLVED"
    code_set = session.query(CodeSetModel).one()
    assert code_set.source_type == "INLINE_ENUM"
    assert code_set.name == "机构名称（内联枚举）"
    assert session.query(CodeValueModel).count() == 2
