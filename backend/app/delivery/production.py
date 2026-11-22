"""Step 3.3：生产 DDL、只读校验规则 SQL 编译与方言安全参数内联（Layer 6: delivery）。

严格区分 Oracle 23 与 MySQL 8 生产物理上限与字符串转义规则：
- Oracle 23：TEXT > 4000 自动升格 CLOB（防 ORA-00910），NUMBER 精度钳位 (1..38, 0..precision)（防 ORA-01727）；
- MySQL 8：VARCHAR > 16383 或宽表行内字节逼近 65535 自动升格 TEXT（防 Error 1118），DECIMAL 钳位 (1..65, 0..30)；
- 参数内联：跳过单引号字符串字面量防止误替 `:word`，MySQL 方言同步转义反斜杠 `\\` 保护正则规则。
"""

import math
import re
from decimal import Decimal
from typing import Any, Dict, List, Optional

from app.generation.core.binding import SchemaBinding
from app.generation.sql.compiler import compile_where, quote_identifier
from app.infra.contracts import RuleDefinition

_MYSQL_ROW_INLINE_BUDGET = 60000
_ORACLE_TOKEN_RE = re.compile(r"'(?:''|[^'])*'|:([A-Za-z_][A-Za-z0-9_]*)")
_MYSQL_TOKEN_RE = re.compile(r"'(?:''|\\.|[^'\\])*'|:([A-Za-z_][A-Za-z0-9_]*)")


def compile_production_oracle_ddl(table: str, schema: List[SchemaBinding]) -> str:
    columns = []
    for field in schema:
        if field.storage_type == "NUMBER":
            precision = max(1, min(int(field.length or 38), 38))
            scale = max(0, min(int(field.decimal_places or 0), precision))
            data_type = f"NUMBER({precision},{scale})"
        else:
            char_len = max(1, int(field.length or 64))
            data_type = "CLOB" if char_len > 4000 else f"VARCHAR2({char_len} CHAR)"
        columns.append(f"{quote_identifier(field.field, 'oracle')} {data_type} NULL")
    return f"CREATE TABLE {quote_identifier(table, 'oracle')} (\n  " + ",\n  ".join(columns) + "\n)"


def compile_production_mysql_ddl(table: str, schema: List[SchemaBinding]) -> str:
    col_specs: List[Dict[str, Any]] = []
    inline_bytes = 0
    for field in schema:
        if field.storage_type == "NUMBER":
            precision = max(1, min(int(field.length or 18), 65))
            scale = max(0, min(int(field.decimal_places or 0), min(30, precision)))
            col_specs.append({"field": field.field, "type": f"DECIMAL({precision},{scale})", "text_len": 0})
            inline_bytes += (precision // 9 + 1) * 4 + 2
        else:
            char_len = max(1, int(field.length or 64))
            if char_len > 16383:
                col_specs.append({"field": field.field, "type": "TEXT", "text_len": 0})
                inline_bytes += 20
            else:
                col_specs.append({"field": field.field, "type": f"VARCHAR({char_len})", "text_len": char_len})
                inline_bytes += char_len * 4 + 2
    if inline_bytes > _MYSQL_ROW_INLINE_BUDGET:
        for idx in sorted(
            (i for i, item in enumerate(col_specs) if item["text_len"] > 0),
            key=lambda i: col_specs[i]["text_len"],
            reverse=True,
        ):
            if inline_bytes <= _MYSQL_ROW_INLINE_BUDGET:
                break
            inline_bytes -= col_specs[idx]["text_len"] * 4 + 2
            inline_bytes += 20
            col_specs[idx]["type"] = "TEXT"
            col_specs[idx]["text_len"] = 0
    columns = [f"{quote_identifier(item['field'])} {item['type']} NULL" for item in col_specs]
    return f"CREATE TABLE IF NOT EXISTS {quote_identifier(table)} (\n  " + ",\n  ".join(columns) + "\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"


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
    return inline_sql_literals(sql, {**params, "compiled_rule_key": rule.rule_key}, dialect=dialect)


def sql_literal(value: Any, dialect: str = "oracle") -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"Non-finite numeric SQL literal is not allowed: {value}")
        return format(value, "f")
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"Non-finite numeric SQL literal is not allowed: {value}")
        return str(value)
    text = str(value)
    if (dialect or "oracle").strip().lower().startswith("mysql"):
        text = text.replace("\\", "\\\\")
    return "'" + text.replace("'", "''") + "'"


def inline_sql_literals(sql: str, params: Dict[str, Any], dialect: str = "oracle") -> str:
    """将命名绑定参数安全内联为方言字面量，跳过单引号字符串字面量内部防止误替换 `:word`。"""
    is_mysql = (dialect or "oracle").strip().lower().startswith("mysql")
    pattern = _MYSQL_TOKEN_RE if is_mysql else _ORACLE_TOKEN_RE

    def replace(match):
        key = match.group(1)
        if key is None:
            return match.group(0)
        if key not in params:
            raise ValueError(f"Missing SQL parameter: {key}")
        return sql_literal(params[key], dialect=dialect)

    return pattern.sub(replace, sql)
