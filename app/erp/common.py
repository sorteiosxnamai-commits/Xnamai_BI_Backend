import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

BR_TZ = ZoneInfo("America/Sao_Paulo")
MISSING = object()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite devolve datetimes ingênuos; normaliza para UTC com tz."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_source_instant(value: Any) -> datetime | None:
    """Datas Mercos sem fuso são horário de Brasília; devolve instante UTC."""
    if value in (None, ""):
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BR_TZ)
    return parsed.astimezone(timezone.utc)


_SECRET_RE = re.compile(
    r"(?i)(api[-_ ]?key|x-api-key|token|authorization|secret|senha|password)(\s*[:=]\s*)([^\s,;}\"']+)"
)
_LONG_BLOB_RE = re.compile(r"\b[A-Za-z0-9+/_-]{32,}={0,2}\b")


def sanitize_remote_message(message: Any, limit: int = 240) -> str | None:
    """Texto curto e seguro de um erro devolvido por sistema externo.

    Extrai o campo legível (`error`/`message`/`detail`) quando a resposta é JSON, mascara
    chaves/tokens e sequências longas e limita o tamanho. Nunca devolve o corpo bruto."""
    if message in (None, ""):
        return None
    text = str(message).strip()
    start = text.find("{")
    if start != -1:
        try:
            body = json.loads(text[start:])
        except ValueError:
            body = None
        if isinstance(body, dict):
            for key in ("error", "message", "detail", "mensagem"):
                if isinstance(body.get(key), str) and body[key].strip():
                    text = (text[:start].rstrip(": ") + ": " if text[:start].strip(": ") else "") + body[key].strip()
                    break
    text = _SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[oculto]", text)
    text = _LONG_BLOB_RE.sub("[oculto]", text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def cursor_not_before(new: str | None, current: str | None) -> bool:
    """True se `new` não é anterior a `current` (cursor de checkpoint nunca regride)."""
    if not new:
        return False
    if not current:
        return True
    a, b = parse_source_instant(new), parse_source_instant(current)
    if a is None or b is None:
        return str(new) >= str(current)
    return a >= b


def parse_source_date(value: Any) -> date | None:
    """Data sem hora permanece data; nunca vira instante UTC inventado."""
    if value in (None, ""):
        return None
    text = str(value).strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def to_decimal(value: Any, *, default: Decimal | None = None) -> Decimal | None:
    if value is None or value == "":
        return default
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, bool):
        raise ValueError(f"Valor numérico inválido: {value!r}")
    elif isinstance(value, (int, float)):
        number = Decimal(str(value))
    else:
        text = str(value).strip()
        if "," in text and "." in text:
            text = text.replace(".", "").replace(",", ".")
        elif "," in text:
            text = text.replace(",", ".")
        try:
            number = Decimal(text)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"Valor numérico inválido: {value!r}") from exc
    if not number.is_finite():
        raise ValueError(f"Valor numérico não finito: {value!r}")
    return number


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def plain_decimal(value: Decimal | None) -> str | None:
    """Decimal sem notação científica e sem zeros à direita (`30.0000` -> `30`, `20.000` -> `20`)."""
    if value is None:
        return None
    text = format(value.normalize(), "f")
    return text


def money(value: Decimal | None) -> str | None:
    """Dinheiro em contrato como string decimal."""
    if value is None:
        return None
    return format(value.quantize(Decimal("0.01")), "f")


def quantity(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.normalize() if value == value.to_integral() else value, "f")


def iso(value: datetime | None) -> str | None:
    value = as_utc(value)
    return value.isoformat() if value else None


def error_detail(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **extra}


def http_error(status: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status, error_detail(code, message, **extra))


class Page:
    def __init__(self, items: list, total: int, page: int, page_size: int):
        self.items = items
        self.total = total
        self.page = page
        self.page_size = page_size

    def envelope(self, serializer, *, sort: str, order: str, filters: dict) -> dict:
        pages = (self.total + self.page_size - 1) // self.page_size if self.total else 0
        return {
            "items": [serializer(item) for item in self.items],
            "page": self.page,
            "pageSize": self.page_size,
            "totalItems": self.total,
            "totalPages": pages,
            "sort": sort,
            "order": order,
            "appliedFilters": {k: v for k, v in filters.items() if v not in (None, "")},
        }


def paginate(
    db: Session,
    query: Select,
    *,
    id_column,
    sort_columns: dict[str, Any],
    sort: str | None,
    default_sort: str,
    order: str,
    page: int,
    page_size: int,
) -> tuple[Page, str, str]:
    """Paginação no servidor: ordenação em allowlist e desempate por ID."""
    key = sort if sort in sort_columns else default_sort
    column = sort_columns[key]
    direction = "asc" if order == "asc" else "desc"
    ordering = column.asc() if direction == "asc" else column.desc()
    tiebreak = id_column.asc() if direction == "asc" else id_column.desc()
    total = int(
        db.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
        or 0
    )
    rows = db.scalars(
        query.order_by(ordering, tiebreak)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return Page(list(rows), total, page, page_size), key, direction


def minus_seconds(value: datetime, seconds: int) -> datetime:
    return value - timedelta(seconds=seconds)
