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

    def drop_tables(self, tables: List[str]) -> List[str]:
        """清理批次专属沙箱表；单表失败不影响其他表，也不触碰生产表。"""
        errors: List[str] = []
        try:
            with self.engine.begin() as connection:
                for table in sorted(set(item for item in tables if item)):
                    try:
                        connection.execute(text(f"DROP TABLE {quote_identifier(table, 'oracle')} PURGE"))
                    except Exception as exc:
                        errors.append(f"{table}: {type(exc).__name__}")
        finally:
            self.engine.dispose()
        return errors
