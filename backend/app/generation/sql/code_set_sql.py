"""码表物理表命名与数据库 DDL / MERGE / INSERT 编译（Layer 5: generation/sql）。

从原 `assets/code_sets/oracle.py` 剥离下沉至生成验证层，确保 Layer 4 `catalog/` 彻底脱离数据库 SQL 方言。
同时覆盖 Oracle 23（MERGE / INSERT ALL）与 MySQL 8（CREATE TABLE / INSERT ... ON DUPLICATE KEY UPDATE）。
"""

import hashlib
import re
from typing import Any, Dict, List

from app.infra.db import quote_identifier

# MySQL 8 标识符硬上限 64 字符（Oracle 23 为 128 字节）；生产码表名取二者交集
_MAX_STABLE_TABLE_LEN = 64
_STABLE_PREFIX = "REG_CS_"
_DIGEST_LEN = 8
# 列宽与 DDL 严格一致：超长值截断，空中文标签回落为码值本身（Oracle 空串即 NULL，会撞 NOT NULL 报 ORA-01400）
_COLUMN_LIMITS = {"CODE_VALUE": 128, "LABEL_CN": 255, "LABEL_EN": 255, "DEFINITION": 2000}
_ORACLE_INSERT_ALL_BATCH = 200
_CODE_SET_COLUMNS = ("CODE_VALUE", "LABEL_CN", "LABEL_EN", "DEFINITION", "SORT_ORDER", "ENABLED")


def stable_code_set_table(code: str) -> str:
    normalized = re.sub(r"[^A-Z0-9_]", "_", code.upper()).strip("_") or "CODE_SET"
    digest = hashlib.sha1(code.encode("utf-8")).hexdigest()[:_DIGEST_LEN].upper()
    keep = _MAX_STABLE_TABLE_LEN - len(_STABLE_PREFIX) - 1 - _DIGEST_LEN
    return f"{_STABLE_PREFIX}{normalized[:keep]}_{digest}"


def sandbox_code_set_table(run_id: str, attempt: int, code: str) -> str:
    run_part = run_id.replace("-", "").upper()[:12]
    return f"DQ_{run_part}_{attempt}_{stable_code_set_table(code)}"


def _clip(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    return text[:limit]


def code_set_value_rows(code_set: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把码表值规范化为与 DDL 列宽一致、Oracle/MySQL 均可安全写入的行（沙箱灌数与生产脚本共用）。"""
    rows = []
    for value in code_set.get("values") or []:
        code_value = _clip(value.get("value"), _COLUMN_LIMITS["CODE_VALUE"])
        if not code_value.strip():
            continue
        label_cn = _clip(value.get("label_cn"), _COLUMN_LIMITS["LABEL_CN"]).strip() or code_value
        rows.append(
            {
                "CODE_VALUE": code_value,
                "LABEL_CN": label_cn,
                "LABEL_EN": _clip(value.get("label_en"), _COLUMN_LIMITS["LABEL_EN"]),
                "DEFINITION": _clip(value.get("definition"), _COLUMN_LIMITS["DEFINITION"]),
                "SORT_ORDER": int(value.get("sort_order") or 0),
                "ENABLED": 1 if value.get("enabled", True) else 0,
            }
        )
    return rows


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


def compile_code_set_mysql_ddl(table: str) -> str:
    quoted = quote_identifier(table, "mysql")
    return f"""CREATE TABLE IF NOT EXISTS {quoted} (
  `CODE_VALUE` VARCHAR(128) NOT NULL PRIMARY KEY,
  `LABEL_CN` VARCHAR(255) NOT NULL,
  `LABEL_EN` VARCHAR(255) NULL,
  `DEFINITION` VARCHAR(2000) NULL,
  `SORT_ORDER` INT NOT NULL,
  `ENABLED` TINYINT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin"""


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _mysql_literal(value: Any) -> str:
    if isinstance(value, str):
        return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"
    return _sql_literal(value)


def compile_code_set_oracle_merge(code_set: Dict[str, Any], table: str) -> str:
    statements = []
    quoted = quote_identifier(table, "oracle")
    for row in code_set_value_rows(code_set):
        select = ", ".join(f"{_sql_literal(item)} AS {name}" for name, item in row.items())
        columns = ", ".join(f'"{name}"' for name in row)
        target_values = ", ".join(f"s.{name}" for name in row)
        updates = ", ".join(f'd."{name}" = s.{name}' for name in row if name != "CODE_VALUE")
        statements.append(
            f"MERGE INTO {quoted} d USING (SELECT {select} FROM dual) s "
            f'ON (d."CODE_VALUE" = s.CODE_VALUE) '
            f"WHEN MATCHED THEN UPDATE SET {updates} "
            f"WHEN NOT MATCHED THEN INSERT ({columns}) VALUES ({target_values})"
        )
    return ";\n".join(statements)


def compile_code_set_oracle_insert(code_set: Dict[str, Any], table: str) -> str:
    """Oracle INSERT ALL：用于首次初始化，按 200 行分批避免超大码表单语句过长；重复部署请使用 MERGE 版本。"""
    quoted = quote_identifier(table, "oracle")
    columns = "(" + ", ".join(f'"{name}"' for name in _CODE_SET_COLUMNS) + ")"
    rows = code_set_value_rows(code_set)
    batches = []
    for start in range(0, len(rows), _ORACLE_INSERT_ALL_BATCH):
        lines = [
            f"  INTO {quoted} {columns} VALUES ({', '.join(_sql_literal(row[name]) for name in _CODE_SET_COLUMNS)})"
            for row in rows[start : start + _ORACLE_INSERT_ALL_BATCH]
        ]
        batches.append("INSERT ALL\n" + "\n".join(lines) + "\nSELECT 1 FROM DUAL")
    return ";\n".join(batches)


def compile_code_set_mysql_upsert(code_set: Dict[str, Any], table: str) -> str:
    """MySQL 8 幂等写入：多行 INSERT ... AS new ON DUPLICATE KEY UPDATE（按 200 行分批）。"""
    quoted = quote_identifier(table, "mysql")
    columns = ", ".join(f"`{name}`" for name in _CODE_SET_COLUMNS)
    updates = ", ".join(f"`{name}` = new.`{name}`" for name in _CODE_SET_COLUMNS if name != "CODE_VALUE")
    rows = code_set_value_rows(code_set)
    statements = []
    for start in range(0, len(rows), _ORACLE_INSERT_ALL_BATCH):
        values = ",\n  ".join(
            "(" + ", ".join(_mysql_literal(row[name]) for name in _CODE_SET_COLUMNS) + ")"
            for row in rows[start : start + _ORACLE_INSERT_ALL_BATCH]
        )
        statements.append(
            f"INSERT INTO {quoted} ({columns}) VALUES\n  {values}\nAS new ON DUPLICATE KEY UPDATE {updates}"
        )
    return ";\n".join(statements)
