from typing import Any, Dict, Optional, Tuple

from .domain import RuleDefinition, SpecDocument


def quote_identifier(name: str, dialect: str = "mysql") -> str:
    if not name.replace("_", "").isalnum() or not name.isascii():
        raise ValueError(f"Unsafe SQL identifier: {name}")
    return f'"{name}"' if dialect == "oracle" else f"`{name}`"


def _blank_fold(field: str, dialect: str) -> str:
    """把制表/换行折成空格再 TRIM。

    只 TRIM 空格会把纯 "\\t" 当成有值：Oracle 的 TRIM 默认只去空格，
    于是「必填」在遇到空白填充的脏数据时漏判。必填的语义是「去掉空白后仍有内容」。
    """
    chars = ("CHR(9)", "CHR(10)", "CHR(13)") if dialect == "oracle" else ("CHAR(9)", "CHAR(10)", "CHAR(13)")
    folded = field
    for char in chars:
        folded = f"REPLACE({folded}, {char}, ' ')"
    return f"TRIM({folded})"


def missing_text(field: str, dialect: str) -> str:
    folded = _blank_fold(field, dialect)
    if dialect == "oracle":
        # Oracle 里空串即 NULL，折叠后 TRIM 为 '' 会变成 NULL
        return f"({field} IS NULL OR {folded} IS NULL)"
    return f"({field} IS NULL OR {folded} = '')"


def compile_where(
    rule: RuleDefinition,
    dialect: str = "mysql",
    physical_tables: Optional[Dict[str, str]] = None,
) -> Tuple[str, Dict[str, Any]]:
    field = f"t.{quote_identifier(rule.field, dialect)}"
    params: Dict[str, Any] = {}
    if rule.operator == "REQUIRED":
        return missing_text(field, dialect), params
    if rule.operator in {"ENUM", "NOT_IN"}:
        values = list(rule.params["values"])
        names = []
        for index, value in enumerate(values):
            key = f"value_{index}"
            params[key] = value
            names.append(f":{key}")
        operator = "NOT IN" if rule.operator == "ENUM" else "IN"
        null_guard = f"{field} IS NOT NULL AND " if rule.operator == "ENUM" else ""
        return f"({null_guard}{field} {operator} ({', '.join(names)}))", params
    if rule.operator == "COND_REQUIRED":
        condition_field = f"t.{quote_identifier(rule.params['if_field'], dialect)}"
        condition_values = []
        for index, value in enumerate(rule.params["if_values"]):
            key = f"if_value_{index}"
            params[key] = value
            condition_values.append(f":{key}")
        condition_operator = "=" if rule.params["if_operator"] == "EQ" else "<>"
        condition = f"{condition_field} {condition_operator} ({', '.join(condition_values)})"
        return (
            f"({condition_field} IS NOT NULL AND {condition} "
            f"AND {missing_text(field, dialect)})",
            params,
        )
    if rule.operator in {"LENGTH", "LENGTH_MAX"}:
        params["expected_length"] = int(rule.params["length"])
        comparison = "<>" if rule.operator == "LENGTH" else ">"
        return f"({field} IS NOT NULL AND LENGTH({field}) {comparison} :expected_length)", params
    if rule.operator == "REGEXP":
        params["expected_pattern"] = rule.params["pattern"]
        if dialect == "oracle":
            return f"({field} IS NOT NULL AND NOT REGEXP_LIKE({field}, :expected_pattern))", params
        return f"({field} IS NOT NULL AND {field} NOT REGEXP :expected_pattern)", params
    if rule.operator == "FOREIGN_KEY":
        reference_code = rule.params["references_table"]
        reference_table = (physical_tables or {}).get(reference_code, reference_code)
        reference_field = quote_identifier(rule.params["references_field"], dialect)
        return (
            f"({field} IS NOT NULL AND NOT EXISTS "
            f"(SELECT 1 FROM {quote_identifier(reference_table, dialect)} r "
            f"WHERE r.{reference_field} = {field}))",
            params,
        )
    raise ValueError(f"Unsupported operator: {rule.operator}")


def compile_rule_query(
    rule: RuleDefinition,
    dialect: str = "mysql",
    physical_tables: Optional[Dict[str, str]] = None,
) -> Tuple[str, Dict[str, Any]]:
    where, params = compile_where(rule, dialect, physical_tables)
    params["compiled_rule_key"] = rule.rule_key
    table = (physical_tables or {}).get(rule.table_code, rule.table_code)
    table_alias = "t" if dialect == "oracle" else "AS t"
    sql = (
        f"SELECT :compiled_rule_key AS rule_key, t.{quote_identifier('record_id', dialect)} AS record_id "
        f"FROM {quote_identifier(table, dialect)} {table_alias} WHERE {where}"
    )
    return sql, params


def compile_mysql_ddl(spec: SpecDocument) -> str:
    columns = ["`record_id` VARCHAR(64) NOT NULL PRIMARY KEY"]
    for field in spec.fields:
        if field.data_type == "NUMBER":
            data_type = f"DECIMAL({field.length},{field.decimal_places or 0})"
        else:
            data_type = f"VARCHAR({field.length})"
        columns.append(f"{quote_identifier(field.name)} {data_type} NULL")
    body = ",\n  ".join(columns)
    return (
        f"CREATE TABLE IF NOT EXISTS {quote_identifier(spec.table_code)} (\n  {body}\n) "
        "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;"
    )
