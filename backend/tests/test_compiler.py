from app.compiler import compile_mysql_ddl, compile_rule_query, quote_identifier
from app.rules import build_explicit_rules


def test_mysql_artifacts_use_whitelisted_identifiers_and_parameters(organization_spec):
    ddl = compile_mysql_ddl(organization_spec)
    assert "CREATE TABLE IF NOT EXISTS `SAFE_6_1_ORGANIZATION`" in ddl
    assert "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4" in ddl
    for rule in build_explicit_rules(organization_spec):
        sql, params = compile_rule_query(rule)
        assert "compiled_rule_key" in params
        assert organization_spec.table_code in sql


def test_oracle_required_handles_empty_string_as_null(organization_spec):
    rule = next(rule for rule in build_explicit_rules(organization_spec) if rule.operator == "REQUIRED")
    sql, _ = compile_rule_query(rule, "oracle")
    assert "IS NULL" in sql
    assert "TRIM" in sql
    assert "= ''" not in sql


def test_identifier_validation():
    try:
        quote_identifier("SAFE_TABLE; DROP TABLE USERS")
    except ValueError:
        pass
    else:
        raise AssertionError("Unsafe identifier was accepted")
