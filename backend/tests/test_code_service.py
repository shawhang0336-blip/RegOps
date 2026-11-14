import uuid

from app.asset_models import CodeValueModel, FieldCodeReferenceModel
from app.code_service import generation_blockers
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
    from app.code_service import refresh_reference_status

    refresh_reference_status(session, reference.code_set_id)
    session.commit()
    assert generation_blockers(session, table) == []
