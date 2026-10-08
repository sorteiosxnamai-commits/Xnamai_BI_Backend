"""Persistência linha a linha de uma página de origem.

Cada linha roda em SAVEPOINT: uma linha inválida vai para a quarentena sem
contaminar o restante da página nem avançar o checkpoint.
"""

from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.erp.common import as_utc, fingerprint, utcnow
from app.erp.config import erp_settings
from app.erp.models.core import (
    ErpFieldInventory,
    ErpOperation,
    ErpQuarantine,
    ErpSourceSnapshot,
)
from app.erp.registry import REGISTRY, ResourceDef, map_row


def _find_entity(db: Session, connection_id: str, definition: ResourceDef, row: dict, key: str):
    if definition.lookup is not None:
        return definition.lookup(db, connection_id, row, key)
    model = definition.model
    return db.scalar(
        select(model).where(
            model.connection_id == connection_id, model.external_id == key
        )
    )


def _replace_children(db: Session, definition: ResourceDef, entity: Any, mapped) -> None:
    from sqlalchemy import delete

    for child in definition.children:
        if child.name not in mapped.children:
            continue
        db.execute(delete(child.model).where(getattr(child.model, child.fk) == entity.id))
        for data in mapped.children[child.name] or []:
            db.add(child.model(**{child.fk: entity.id}, **data))


def _resolve_quarantine(db: Session, connection_id: str, resource: str, key: str) -> None:
    db.execute(
        update(ErpQuarantine)
        .where(
            ErpQuarantine.connection_id == connection_id,
            ErpQuarantine.resource == resource,
            ErpQuarantine.external_key == key,
            ErpQuarantine.resolved_at.is_(None),
        )
        .values(resolved_at=utcnow())
    )


def _confirm_operations(db: Session, connection_id: str, resource: str, key: str) -> None:
    """Retorno da origem confirma o outbox; nunca gera nova escrita."""
    db.execute(
        update(ErpOperation)
        .where(
            ErpOperation.connection_id == connection_id,
            ErpOperation.target_resource == resource,
            ErpOperation.external_id == key,
            ErpOperation.status == "succeeded",
            ErpOperation.mirror_confirmed_at.is_(None),
        )
        .values(mirror_confirmed_at=utcnow())
    )


def quarantine(
    db: Session,
    connection_id: str,
    resource: str,
    key: str,
    reason: str,
    payload: dict | None,
    run_id: int | None,
) -> None:
    row = db.scalar(
        select(ErpQuarantine).where(
            ErpQuarantine.connection_id == connection_id,
            ErpQuarantine.resource == resource,
            ErpQuarantine.external_key == key,
        )
    )
    if row is None:
        db.add(
            ErpQuarantine(
                connection_id=connection_id,
                resource=resource,
                external_key=key,
                run_id=run_id,
                reason=reason[:2000],
                payload=payload,
            )
        )
    else:
        row.reason = reason[:2000]
        row.payload = payload
        row.attempts = (row.attempts or 0) + 1
        row.run_id = run_id
        row.resolved_at = None
        db.add(row)


def process_row(
    db: Session,
    connection_id: str,
    definition: ResourceDef,
    row: dict,
    run_id: int | None,
) -> str:
    """Devolve: persisted | unchanged | stale. Levanta ValueError se inválida."""
    key = definition.key(row)
    fp = fingerprint(row)
    entity = _find_entity(db, connection_id, definition, row, key)
    if entity is not None and entity.fingerprint == fp:
        _resolve_quarantine(db, connection_id, definition.alias, key)
        return "unchanged"

    mapped = map_row(definition, row)
    incoming = as_utc(mapped.values.get("source_updated_at"))
    current = as_utc(entity.source_updated_at) if entity is not None else None
    stale = bool(entity is not None and incoming and current and incoming < current)

    snapshot_exists = db.scalar(
        select(ErpSourceSnapshot.id).where(
            ErpSourceSnapshot.connection_id == connection_id,
            ErpSourceSnapshot.resource == definition.alias,
            ErpSourceSnapshot.external_key == key,
            ErpSourceSnapshot.fingerprint == fp,
        )
    )
    if not snapshot_exists:
        db.add(
            ErpSourceSnapshot(
                connection_id=connection_id,
                resource=definition.alias,
                external_key=key,
                source_version=(incoming.isoformat() if incoming else None),
                fingerprint=fp,
                payload=row,
                retention_until=utcnow()
                + timedelta(days=erp_settings().erp_snapshot_retention_days),
            )
        )
    if stale:
        # Versão antiga atrasada não regride a entidade mais nova.
        return "stale"

    if entity is None:
        entity = definition.model(connection_id=connection_id, external_id=key)
    for attr, value in mapped.values.items():
        setattr(entity, attr, value)
    entity.fingerprint = fp
    entity.captured_at = utcnow()
    entity.version = (entity.version or 0) + 1
    db.add(entity)
    db.flush()
    _replace_children(db, definition, entity, mapped)
    if definition.finalize is not None:
        definition.finalize(entity, row, definition)
        db.add(entity)
    _resolve_quarantine(db, connection_id, definition.alias, key)
    _confirm_operations(db, connection_id, definition.alias, key)
    return "persisted"


def _typed_keys(row: dict) -> dict[str, str]:
    typed: dict[str, str] = {}
    for key, value in row.items():
        typed[str(key)] = type(value).__name__
        if isinstance(value, list):
            for element in value[:200]:
                if isinstance(element, dict):
                    for inner, inner_value in element.items():
                        typed[f"{key}[].{inner}"] = type(inner_value).__name__
    return typed


def update_field_inventory(
    db: Session, connection_id: str, definition: ResourceDef, rows: list[dict]
) -> list[str]:
    """Registra todo campo visto; devolve os campos ainda sem mapeamento."""
    seen: dict[str, tuple[int, str]] = {}
    for row in rows:
        for source_key, kind in _typed_keys(row).items():
            count, previous = seen.get(source_key, (0, kind))
            seen[source_key] = (count + 1, previous if kind == "NoneType" else kind)
    known = definition.known_keys
    child_known: set[str] = set()
    for child in definition.children:
        for name in child.keys:
            for field in child.fields:
                child_known.update(f"{name}[].{key}" for key in field.keys)
            child_known.add(f"{name}[].id")
            child_known.add(f"{name}[].excluido")
    now = utcnow()
    unmapped: list[str] = []
    existing = {
        item.source_key: item
        for item in db.scalars(
            select(ErpFieldInventory).where(
                ErpFieldInventory.connection_id == connection_id,
                ErpFieldInventory.resource == definition.alias,
                ErpFieldInventory.source_key.in_(list(seen)),
            )
        )
    }
    child_names = {name for child in definition.children for name in child.keys}
    for source_key, (count, kind) in seen.items():
        if "[]." in source_key:
            parent = source_key.split("[].", 1)[0]
            # Lista filha: confere campo a campo. Lista guardada inteira
            # (e-mails, extras, tags): o campo de topo mapeado cobre o conteúdo.
            mapped = (
                source_key in child_known
                if parent in child_names
                else parent in known
            )
        else:
            mapped = source_key in known
        item = existing.get(source_key)
        if item is None:
            item = ErpFieldInventory(
                connection_id=connection_id,
                resource=definition.alias,
                source_key=source_key,
                first_seen_at=now,
                seen_count=0,
            )
        item.seen_count = (item.seen_count or 0) + count
        item.last_seen_at = now
        item.mapped = mapped
        item.sample_type = kind
        db.add(item)
        if not mapped:
            unmapped.append(source_key)
    return unmapped


def definition_for(alias: str) -> ResourceDef:
    try:
        return REGISTRY[alias]
    except KeyError as exc:
        raise ValueError(f"Recurso desconhecido: {alias}") from exc
