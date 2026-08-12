from sqlalchemy import create_engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import NullPool
from app.config import settings


def _build_engine():
    url = settings().database_url
    kwargs = {"pool_pre_ping": True}

    if url.startswith("postgresql"):
        parsed = make_url(url)
        connect_args = {}
        # Supavisor transaction pooler (6543) does not support prepared statements
        if parsed.port == 6543 or "pooler.supabase.com" in (parsed.host or ""):
            kwargs["poolclass"] = NullPool
            connect_args["prepare_threshold"] = None
        if connect_args:
            kwargs["connect_args"] = connect_args

    return create_engine(url, **kwargs)


engine = _build_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
