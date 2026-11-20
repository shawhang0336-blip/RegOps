from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.infra.db import Base


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
