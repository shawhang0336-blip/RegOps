import re
from typing import Any, Dict, List, Optional

from app.engine.compiler import compile_where, quote_identifier
from app.engine.domain import RuleDefinition
from app.engine.binding import SchemaBinding


def compile_production_oracle_ddl(table: str, schema: List[SchemaBinding]) -> str:
    columns = []
    for field in schema:
        data_type = f"NUMBER({field.length},{field.decimal_places})" if field.storage_type == "NUMBER" else f"VARCHAR2({field.length} CHAR)"
        columns.append(f"{quote_identifier(field.field, 'oracle')} {data_type} NULL")
    return f"CREATE TABLE {quote_identifier(table, 'oracle')} (\n  " + ",\n  ".join(columns) + "\n)"


def compile_production_mysql_ddl(table: str, schema: List[SchemaBinding]) -> str:
    columns = []
    for field in schema:
        data_type = f"DECIMAL({field.length},{field.decimal_places})" if field.storage_type == "NUMBER" else f"VARCHAR({field.length})"
        columns.append(f"{quote_identifier(field.field)} {data_type} NULL")
    return f"CREATE TABLE {quote_identifier(table)} (\n  " + ",\n  ".join(columns) + "\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"


def compile_production_rule_query(
    rule: RuleDefinition,
    dialect: str = "oracle",
    code_set_bindings: Optional[Dict[Any, Dict[str, Any]]] = None,
) -> str:
    where, params = compile_where(rule, dialect, code_set_bindings=code_set_bindings)
    alias = "t" if dialect == "oracle" else "AS t"
    sql = (
        f"SELECT :compiled_rule_key AS rule_key, t.* "
        f"FROM {quote_identifier(rule.table_code, dialect)} {alias} WHERE {where}"
    )
    return inline_sql_literals(sql, {**params, "compiled_rule_key": rule.rule_key})


def sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def inline_sql_literals(sql: str, params: Dict[str, Any]) -> str:
    def replace(match):
        key = match.group(1)
        if key not in params:
            raise ValueError(f"Missing SQL parameter: {key}")
        return sql_literal(params[key])

    return re.sub(r":([A-Za-z_][A-Za-z0-9_]*)", replace, sql)
