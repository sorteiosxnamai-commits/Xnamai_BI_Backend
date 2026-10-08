"""Metadata e sessão do ERP, separados do `Base` legado.

O `Base` legado sofre `create_all` no lifespan do app. O ERP só é criado por
Alembic, por isso usa `ErpBase` com metadata próprio.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager

from sqlalchemy import MetaData, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class ErpBase(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


_factory: Callable[[], Session] | None = None


def set_session_factory(factory: Callable[[], Session] | None) -> None:
    """Permite aos testes e ao worker trocarem a fábrica de sessões."""
    global _factory
    _factory = factory


def session_factory() -> Callable[[], Session]:
    if _factory is not None:
        return _factory
    from app.database import engine

    return sessionmaker(bind=engine, expire_on_commit=False)


def _sqlite_begin(session, transaction, connection) -> None:
    """pysqlite só emite BEGIN antes de DML. Sem isto, um SAVEPOINT iniciaria a
    transação e o RELEASE faria COMMIT, quebrando a atomicidade de uma página
    (no PostgreSQL a transação já está aberta e isto não é necessário)."""
    if transaction.nested:
        return  # SAVEPOINT dentro de uma transação já aberta
    raw = connection.connection.driver_connection
    if not getattr(raw, "in_transaction", False):
        connection.exec_driver_sql("BEGIN")


def new_session() -> Session:
    db = session_factory()()
    if db.get_bind().dialect.name == "sqlite":
        event.listen(db, "after_begin", _sqlite_begin)
    return db


@contextmanager
def session_scope() -> Iterator[Session]:
    db = new_session()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def erp_db() -> Iterator[Session]:
    """Dependência FastAPI. Quem muda dados chama `commit` explicitamente."""
    db = new_session()
    try:
        yield db
    finally:
        db.close()
