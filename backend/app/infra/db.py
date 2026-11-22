"""数据库连接池、会话工厂、迁移入口与跨方言 SQL 标识符转义（基础设施层 Layer 0）。

面向真实 MySQL / Oracle 生产库配置连接池防断连与并发保护（`pool_pre_ping`、`pool_recycle=1800`、
`pool_size=10`、`max_overflow=20`、`pool_timeout=30`）；仅在离线单测使用 SQLite 时启用 WAL。
"""

from pathlib import Path
from typing import Any, Dict

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.infra.config import get_settings


def _build_engine_kwargs(database_url: str) -> Dict[str, Any]:
    """按方言构造 SQLAlchemy Engine 参数：MySQL/Oracle 配齐生产连接池保护。"""
    if database_url.startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False}, "pool_pre_ping": True}
    return {
        "pool_pre_ping": True,
        "pool_recycle": 1800,
        "pool_size": 10,
        "max_overflow": 20,
        "pool_timeout": 30,
    }


settings = get_settings()
if settings.database_url.startswith("sqlite"):
    database_path = settings.database_url.split("///", 1)[-1]
    if database_path != ":memory:":
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(settings.database_url, **_build_engine_kwargs(settings.database_url))

if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def quote_identifier(name: str, dialect: str = "mysql") -> str:
    """跨方言安全 SQL 标识符转义（Oracle 双引号 / MySQL 反引号），防 SQL 注入与方言大小写错配。"""
    if not isinstance(name, str) or not name or not name.replace("_", "").isalnum() or not name.isascii():
        raise ValueError(f"Unsafe SQL identifier: {name}")
    normalized = (dialect or "mysql").strip().lower()
    if normalized.startswith("oracle"):
        return f'"{name}"'
    if normalized.startswith("mysql") or normalized.startswith("sqlite"):
        return f"`{name}`"
    raise ValueError(f"Unsupported SQL dialect: {dialect}")


def init_db() -> None:
    from alembic import command
    from alembic.config import Config

    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "migrations"))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(config, "head")
