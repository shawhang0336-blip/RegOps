"""码表物理表命名与数据库 DDL / MERGE / INSERT 编译（Layer 5: Engine）。

从原 `assets/code_sets/oracle.py` 剥离下沉至引擎层，确保 Layer 4 `catalog/` 彻底脱离数据库 SQL 方言。
"""

import hashlib
import re
from typing import Any, Dict

from app.infra.db import quote_identifier


def stable_code_set_table(code: str) -> str:
    normalized = re.sub(r"[^A-Z0-9_]", "_", code.upper()).strip("_") or "CODE_SET"
    digest = hashlib.sha1(code.encode("utf-8")).hexdigest()[:8].upper()
    return f"REG_CS_{normalized[:80]}_{digest}"


def sandbox_code_set_table(run_id: str, attempt: int, code: str) -> str:
    run_part = run_id.replace("-", "").upper()[:12]
    return f"DQ_{run_part}_{attempt}_{stable_code_set_table(code)}"


def compile_code_set_oracle_ddl(table: str) -> str:
    quoted = quote_identifier(table, "oracle")
    return f"""CREATE TABLE {quoted} (
  "CODE_VALUE" VARCHAR2(128 CHAR) PRIMARY KEY,
  "LABEL_CN" VARCHAR2(255 CHAR) NOT NULL,
  "LABEL_EN" VARCHAR2(255 CHAR) NULL,
  "DEFINITION" VARCHAR2(2000 CHAR) NULL,
  "SORT_ORDER" NUMBER(10,0) NOT NULL,
  "ENABLED" NUMBER(1,0) NOT NULL
)"""


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def compile_code_set_oracle_merge(code_set: Dict[str, Any], table: str) -> str:
    statements = []
    quoted = quote_identifier(table, "oracle")
    for value in code_set["values"]:
        source_values = {
            "CODE_VALUE": value["value"],
            "LABEL_CN": value["label_cn"],
            "LABEL_EN": value.get("label_en") or "",
            "DEFINITION": value.get("definition") or "",
            "SORT_ORDER": int(value.get("sort_order", 0)),
            "ENABLED": 1 if value.get("enabled", True) else 0,
        }
        select = ", ".join(f"{_sql_literal(item)} AS {name}" for name, item in source_values.items())
        columns = ", ".join(f'"{name}"' for name in source_values)
        target_values = ", ".join(f"s.{name}" for name in source_values)
        updates = ", ".join(f'd."{name}" = s.{name}' for name in source_values if name != "CODE_VALUE")
        statements.append(
            f"MERGE INTO {quoted} d USING (SELECT {select} FROM dual) s "
            f'ON (d."CODE_VALUE" = s.CODE_VALUE) '
            f"WHEN MATCHED THEN UPDATE SET {updates} "
            f"WHEN NOT MATCHED THEN INSERT ({columns}) VALUES ({target_values})"
        )
    return ";\n".join(statements)


def compile_code_set_oracle_insert(code_set: Dict[str, Any], table: str) -> str:
    """Oracle INSERT ALL：用于首次初始化，重复部署请使用 MERGE 版本。"""
    quoted = quote_identifier(table, "oracle")
    columns = '("CODE_VALUE", "LABEL_CN", "LABEL_EN", "DEFINITION", "SORT_ORDER", "ENABLED")'
    statements = []
    for value in code_set["values"]:
        items = (
            value["value"],
            value["label_cn"],
            value.get("label_en") or "",
            value.get("definition") or "",
            int(value.get("sort_order", 0)),
            1 if value.get("enabled", True) else 0,
        )
        values = ", ".join(_sql_literal(item) for item in items)
        statements.append(f"  INTO {quoted} {columns} VALUES ({values})")
    if not statements:
        return ""
    return "INSERT ALL\n" + "\n".join(statements) + "\nSELECT 1 FROM DUAL"
