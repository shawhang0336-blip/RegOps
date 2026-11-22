import uuid

import pytest

from app.generation.core.binding import bind_schema
from app.kernel.event_log import append_event
from app.infra.db_tables import RunModel
from app.delivery.production import (
    compile_production_mysql_ddl,
    compile_production_oracle_ddl,
    compile_production_rule_query,
    inline_sql_literals,
)
from app.delivery.production_service import build_production_bundle
from app.generation.core.rules import build_explicit_rules
from app.infra.errors import WorkflowError
from .helpers import make_session


def test_production_sql_has_no_sandbox_artifacts(organization_spec):
    schema = bind_schema(
        organization_spec,
        [
            {"field": field.name, "storage_type": "NUMBER" if field.data_type == "NUMBER" else "TEXT", "length": field.length, "decimal_places": 0}
            for field in organization_spec.fields
        ],
    )
    oracle = compile_production_oracle_ddl(organization_spec.table_code, schema)
    mysql = compile_production_mysql_ddl(organization_spec.table_code, schema)
    assert "record_id" not in oracle
    assert "record_id" not in mysql
    assert '"CUSTNAME" VARCHAR2(128 CHAR)' in oracle
    for rule in build_explicit_rules(organization_spec):
        sql = compile_production_rule_query(rule, "oracle")
        assert organization_spec.table_code in sql
        assert "record_id" not in sql
        assert ":compiled_rule_key" not in sql
        assert ":value_" not in sql


def test_sql_literal_inlining_escapes_quotes():
    sql = inline_sql_literals("SELECT * FROM t WHERE code = :code", {"code": "X'); DROP TABLE users; --"})
    assert "'X''); DROP TABLE users; --'" in sql


def test_failed_audit_cannot_export():
    session = make_session()
    run = RunModel(id=str(uuid.uuid4()), spec_id=str(uuid.uuid4()))
    session.add(run)
    session.flush()
    append_event(session, run, "AUDIT_REJECTED", "audit", {"result": {"audit_summary": {"all_passed": False}}, "node": "audit"})
    session.commit()
    with pytest.raises(WorkflowError):
        build_production_bundle(session, run, "/tmp/not-used")
