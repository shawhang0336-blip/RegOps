from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import get_settings

settings = get_settings()
if settings.database_url.startswith("sqlite"):
    database_path = settings.database_url.split("///", 1)[-1]
    if database_path != ":memory:":
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    from alembic import command
    from alembic.config import Config

    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(config, "head")
