"""业务库只读抽样探查连接器（Layer 6: delivery）。

生产安全护栏：
1. 严格白名单表校验（大小写不敏感匹配，但在 MySQL 8 Linux 区分大小写环境下保留配置原始大小写）；
2. 强制 `1 <= limit <= 500` 上限，防止服务层直调拖垮业务库；
3. 在 Oracle 23 / MySQL 8 上显式开启只读事务并在退出时回滚，杜绝任何隐式写入；
4. 连接关闭前读取 Oracle CLOB/BLOB（`.read()`）并转换二进制，防止 FastAPI JSON 序列化崩溃。
"""

import re
from typing import Any, Dict, List

from sqlalchemy import create_engine, text

from app.infra.config import Settings

_MAX_PREVIEW_LIMIT = 500
_SAFE_TABLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]*$")


class BusinessDatabaseError(RuntimeError):
    pass


def _serialize_cell(value: Any) -> Any:
    if hasattr(value, "read"):
        value = value.read()
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return value


class ReadOnlyBusinessDatabase:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _resolve_allowed_table(self, table: str, is_oracle: bool) -> str:
        raw_input = (table or "").strip()
        upper_key = raw_input.upper()
        configured_raw = (getattr(self.settings, "business_allowed_tables", "") or "").split(",")
        raw_by_upper = {item.strip().upper(): item.strip() for item in configured_raw if item.strip()}
        allowed_upper = set(raw_by_upper) | set(self.settings.allowed_business_tables)
        if not upper_key or upper_key not in allowed_upper:
            raise BusinessDatabaseError("Table is not in BUSINESS_ALLOWED_TABLES")
        resolved = upper_key if is_oracle else raw_by_upper.get(upper_key, raw_input)
        if not _SAFE_TABLE_RE.fullmatch(resolved):
            raise BusinessDatabaseError("Unsafe table identifier")
        return resolved

    def preview(self, table: str, limit: int = 20) -> List[Dict[str, Any]]:
        if not self.settings.business_database_url:
            raise BusinessDatabaseError("BUSINESS_DATABASE_URL is not configured")
        try:
            row_limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise BusinessDatabaseError("Preview limit must be an integer") from exc
        if row_limit < 1 or row_limit > _MAX_PREVIEW_LIMIT:
            raise BusinessDatabaseError(f"Preview limit must be between 1 and {_MAX_PREVIEW_LIMIT}")

        # 预检白名单（非 Oracle 保留原大小写；连库后按真实 dialect 决定最终大小写与引号）
        self._resolve_allowed_table(table, is_oracle=False)

        engine = create_engine(self.settings.business_database_url, pool_pre_ping=True)
        try:
            with engine.connect() as connection:
                dialect_name = (connection.dialect.name or "").lower()
                is_oracle = dialect_name.startswith("oracle")
                is_mysql = dialect_name.startswith("mysql")
                resolved_table = self._resolve_allowed_table(table, is_oracle=is_oracle)
                if is_oracle:
                    connection.execute(text("SET TRANSACTION READ ONLY"))
                    statement = text(f'SELECT * FROM "{resolved_table}" WHERE ROWNUM <= :row_limit')
                else:
                    if is_mysql:
                        connection.execute(text("START TRANSACTION READ ONLY"))
                    statement = text(f"SELECT * FROM `{resolved_table}` LIMIT :row_limit")
                try:
                    result = connection.execute(statement, {"row_limit": row_limit})
                    return [
                        {k: _serialize_cell(v) for k, v in row.items()}
                        for row in result.mappings().all()
                    ]
                finally:
                    if is_oracle or is_mysql:
                        connection.rollback()
        finally:
            engine.dispose()
