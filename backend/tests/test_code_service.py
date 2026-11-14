import uuid
from openpyxl import Workbook

from app.asset_models import CodeSetModel, CodeValueModel, FieldCodeReferenceModel, RawBlockModel, SourceDocumentModel, TableMetadataModel
from app.code_service import auto_link_all_references, extract_parallel_for_blocks, generation_blockers, normalize_code_set_name, refresh_reference_status
from app.config import Settings
from app.extractors import extract_xlsx
from app.metadata_service import publish_table
from .test_metadata_service import make_metadata
from .test_workflow import make_session


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
    session = make_session()
    monkeypatch.setattr("app.code_service.SessionLocal", lambda: session)
    monkeypatch.setattr("app.metadata_service.SessionLocal", lambda: session)

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

    result = extract_parallel_for_blocks(block_ids, Settings(), targets=["metadata", "code_sets"])
    assert len(result["table_ids"]) == 1
    assert len(result["code_set_ids"]) == 1
    assert result["resolved_reference_count"] == 1
    assert session.query(TableMetadataModel).count() == 1
    assert session.query(CodeSetModel).count() == 1
    assert session.query(CodeValueModel).count() == 2
