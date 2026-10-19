"""Data civil do pedido, inclusive para linhas antigas sem ``data_emissao``."""

from sqlalchemy import Date, func
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.functions import GenericFunction


class brazil_day(GenericFunction):
    type = Date()
    inherit_cache = True


@compiles(brazil_day, "postgresql")
def _postgres_brazil_day(element, compiler, **kw):
    instant = compiler.process(list(element.clauses)[0], **kw)
    return f"DATE(timezone('America/Sao_Paulo', {instant}))"


@compiles(brazil_day, "sqlite")
def _sqlite_brazil_day(element, compiler, **kw):
    instant = compiler.process(list(element.clauses)[0], **kw)
    # Os pedidos nesta integração começam após o fim do horário de verão brasileiro.
    return f"DATE({instant}, '-3 hours')"


def effective_issue_date(order):
    """Prioriza emissão; usa criação Mercos para pedidos antigos sem emissão."""
    return func.coalesce(order.issue_date, brazil_day(order.source_created_at))
