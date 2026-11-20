import re
from typing import Any, Dict, List

from sqlalchemy import create_engine, text

from app.infra.config import Settings


class BusinessDatabaseError(RuntimeError):
    pass


class ReadOnlyBusinessDatabase:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def preview(self, table: str, limit: int = 20) -> List[Dict[str, Any]]:
        if not self.settings.business_database_url:
            raise BusinessDatabaseError("BUSINESS_DATABASE_URL is not configured")
        table = table.upper()
        if table not in self.settings.allowed_business_tables:
            raise BusinessDatabaseError("Table is not in BUSINESS_ALLOWED_TABLES")
        if not re.fullmatch(r"[A-Z][A-Z0-9_$#]*", table):
            raise BusinessDatabaseError("Unsafe table identifier")
        engine = create_engine(self.settings.business_database_url, pool_pre_ping=True)
        try:
            with engine.connect() as connection:
                if engine.dialect.name == "oracle":
                    statement = text(f'SELECT * FROM "{table}" WHERE ROWNUM <= :row_limit')
                else:
                    statement = text(f"SELECT * FROM `{table}` LIMIT :row_limit")
                result = connection.execute(statement, {"row_limit": limit})
                return [dict(row) for row in result.mappings().all()]
        finally:
            engine.dispose()
