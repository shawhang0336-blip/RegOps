from typing import Dict, List

from sqlalchemy import create_engine, text

from .compiler import quote_identifier


def physical_table_name(run_id: str, attempt: int, table_code: str) -> str:
    run_part = run_id.replace("-", "").upper()[:12]
    return f"DQ_{run_part}_{attempt}_{table_code}"


class OracleSandbox:
    def __init__(self, database_url: str) -> None:
        self.engine = create_engine(database_url, pool_pre_ping=True)

    def preview(self, physical_table: str, limit: int = 20) -> List[Dict]:
        quoted = quote_identifier(physical_table, "oracle")
        try:
            with self.engine.connect() as connection:
                result = connection.execute(text(f"SELECT * FROM {quoted} WHERE ROWNUM <= :row_limit"), {"row_limit": limit})
                return [dict(row) for row in result.mappings().all()]
        finally:
            self.engine.dispose()
