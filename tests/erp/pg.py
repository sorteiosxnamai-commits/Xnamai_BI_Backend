"""Apoio a testes em PostgreSQL real (ERP_TEST_DATABASE_URL)."""

import os

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

PG_URL = os.environ.get("ERP_TEST_DATABASE_URL", "")


def fresh_database(name: str) -> str:
    """Recria um banco vazio e devolve a URL dele (só com PostgreSQL configurado)."""
    admin = create_engine(PG_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    return make_url(PG_URL).set(database=name).render_as_string(hide_password=False)
