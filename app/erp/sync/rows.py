"""Persistência linha a linha de uma página de origem.

Cada linha roda em SAVEPOINT: uma linha inválida vai para a quarentena sem
contaminar o restante da página nem avançar o checkpoint.
"""

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select, update
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

log = logging.getLogger("uvicorn.error")


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


def _confirm_operations(
    db: Session, connection_id: str, resource: str, key: str, mirror_cancelled: bool = False
) -> None:
    """Retorno da origem confirma o outbox; nunca gera nova escrita.

    Cancelamento só é confirmado quando o ESPELHO mostra o pedido cancelado: resposta 2xx do
    Mercos não basta (o pedido pode ter sido recusado depois ou o espelho ainda estar antigo)."""
    query = update(ErpOperation).where(
        ErpOperation.connection_id == connection_id,
        ErpOperation.target_resource == resource,
        or_(ErpOperation.external_id == key, ErpOperation.target_external_id == key),
        ErpOperation.status == "succeeded",
        ErpOperation.mirror_confirmed_at.is_(None),
    )
    if not mirror_cancelled:
        query = query.where(ErpOperation.kind != "cancel_order")
    db.execute(query.values(mirror_confirmed_at=utcnow()))


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
    _confirm_operations(db, connection_id, definition.alias, key, getattr(entity, "kind", None) == "cancelled")
    return "persisted"


def persist_rows(
    db: Session,
    connection_id: str,
    definition: ResourceDef,
    rows: list[dict],
    run_id: int | None,
) -> tuple[int, int, int]:
    """Grava uma página. Devolve (persistidos, inalterados, em quarentena).

    Caminho rápido: poucas consultas por PÁGINA (pré-carga de entidades, snapshots e quarentena;
    um flush em lote; DELETE/UPDATE em massa) em vez de ~10 comandos por linha. Com latência de
    rede até o banco, o custo por comando domina; o lote reduz o tempo da carga na mesma
    proporção. Qualquer falha do lote desfaz a página (SAVEPOINT) e refaz linha a linha, com o
    isolamento por linha de sempre: o resultado é idêntico, só mais lento."""
    if definition.lookup is None and len(rows) > 1:
        try:
            with db.begin_nested():
                return _persist_batch(db, connection_id, definition, rows, run_id)
        except Exception:  # noqa: BLE001
            log.warning(
                "ERP: lote de %s falhou; refazendo a página linha a linha", definition.alias, exc_info=True
            )
    return _persist_rowwise(db, connection_id, definition, rows, run_id)


def _persist_rowwise(
    db: Session,
    connection_id: str,
    definition: ResourceDef,
    rows: list[dict],
    run_id: int | None,
) -> tuple[int, int, int]:
    persisted = unchanged = quarantined = 0
    resource = definition.alias
    for row in rows:
        fallback_key = f"sem-id:{fingerprint(row)[:16]}"
        try:
            key = definition.key(row)
        except ValueError as exc:
            quarantine(db, connection_id, resource, fallback_key, str(exc), row, run_id)
            quarantined += 1
            continue
        try:
            with db.begin_nested():
                outcome = process_row(db, connection_id, definition, row, run_id)
        except Exception as exc:  # noqa: BLE001 - qualquer falha de linha isola a linha
            quarantine(db, connection_id, resource, key, f"{type(exc).__name__}: {exc}", row, run_id)
            quarantined += 1
            continue
        if outcome == "persisted":
            persisted += 1
        else:
            unchanged += 1
    return persisted, unchanged, quarantined


def _persist_batch(
    db: Session,
    connection_id: str,
    definition: ResourceDef,
    rows: list[dict],
    run_id: int | None,
) -> tuple[int, int, int]:
    from sqlalchemy import delete

    resource = definition.alias
    model = definition.model
    quarantined = 0
    items: list[tuple[dict, str, str]] = []
    for row in rows:
        try:
            key = definition.key(row)
        except ValueError as exc:
            quarantine(
                db, connection_id, resource, f"sem-id:{fingerprint(row)[:16]}", str(exc), row, run_id
            )
            quarantined += 1
            continue
        items.append((row, key, fingerprint(row)))
    if not items:
        return 0, 0, quarantined

    keys = list({key for _, key, _ in items})
    entities = {
        entity.external_id: entity
        for entity in db.scalars(
            select(model).where(model.connection_id == connection_id, model.external_id.in_(keys))
        )
    }
    snapshots = {
        (external_key, fp)
        for external_key, fp in db.execute(
            select(ErpSourceSnapshot.external_key, ErpSourceSnapshot.fingerprint).where(
                ErpSourceSnapshot.connection_id == connection_id,
                ErpSourceSnapshot.resource == resource,
                ErpSourceSnapshot.external_key.in_(keys),
            )
        )
    }
    pending_quarantine = set(
        db.scalars(
            select(ErpQuarantine.external_key).where(
                ErpQuarantine.connection_id == connection_id,
                ErpQuarantine.resource == resource,
                ErpQuarantine.external_key.in_(keys),
                ErpQuarantine.resolved_at.is_(None),
            )
        )
    )
    retention = timedelta(days=erp_settings().erp_snapshot_retention_days)
    persisted = unchanged = 0
    resolve_keys: set[str] = set()
    confirm_keys: set[str] = set()
    plan: dict[str, tuple[Any, Any]] = {}

    for row, key, fp in items:
        entity = entities.get(key)
        if entity is not None and entity.fingerprint == fp:
            if key in pending_quarantine:
                resolve_keys.add(key)
            unchanged += 1
            continue
        try:
            mapped = map_row(definition, row)
        except Exception as exc:  # noqa: BLE001 - linha inválida vai para a quarentena
            quarantine(db, connection_id, resource, key, f"{type(exc).__name__}: {exc}", row, run_id)
            quarantined += 1
            continue
        incoming = as_utc(mapped.values.get("source_updated_at"))
        current = as_utc(entity.source_updated_at) if entity is not None else None
        stale = bool(entity is not None and incoming and current and incoming < current)
        if (key, fp) not in snapshots:
            snapshots.add((key, fp))
            db.add(
                ErpSourceSnapshot(
                    connection_id=connection_id,
                    resource=resource,
                    external_key=key,
                    source_version=(incoming.isoformat() if incoming else None),
                    fingerprint=fp,
                    payload=row,
                    retention_until=utcnow() + retention,
                )
            )
        if stale:
            unchanged += 1  # versão antiga atrasada não regride a entidade mais nova
            continue
        created = entity is None
        if created:
            entity = model(connection_id=connection_id, external_id=key)
        for attr, value in mapped.values.items():
            setattr(entity, attr, value)
        entity.fingerprint = fp
        entity.captured_at = utcnow()
        entity.version = (entity.version or 0) + 1
        if definition.finalize is not None:
            try:
                definition.finalize(entity, row, definition)
            except Exception as exc:  # noqa: BLE001
                if not created:
                    db.expire(entity)
                quarantine(db, connection_id, resource, key, f"{type(exc).__name__}: {exc}", row, run_id)
                quarantined += 1
                continue
        db.add(entity)
        entities[key] = entity
        plan[key] = (entity, mapped)  # repetição do mesmo id na página: vale a última
        persisted += 1
        if key in pending_quarantine:
            resolve_keys.add(key)
        confirm_keys.add(key)

    db.flush()  # entidades novas/alteradas em lote; atribui os ids

    for child in definition.children:
        fk = getattr(child.model, child.fk)
        targets = [
            (entity, mapped.children[child.name])
            for entity, mapped in plan.values()
            if child.name in mapped.children
        ]
        if not targets:
            continue
        db.execute(delete(child.model).where(fk.in_([entity.id for entity, _ in targets])))
        for entity, children in targets:
            for data in children or []:
                db.add(child.model(**{child.fk: entity.id}, **data))
    if resolve_keys:
        db.execute(
            update(ErpQuarantine)
            .where(
                ErpQuarantine.connection_id == connection_id,
                ErpQuarantine.resource == resource,
                ErpQuarantine.external_key.in_(list(resolve_keys)),
                ErpQuarantine.resolved_at.is_(None),
            )
            .values(resolved_at=utcnow())
        )
    if confirm_keys:
        cancelled_keys = {k for k in confirm_keys if getattr(plan[k][0], "kind", None) == "cancelled"}
        base = update(ErpOperation).where(
            ErpOperation.connection_id == connection_id,
            ErpOperation.target_resource == resource,
            or_(
                ErpOperation.external_id.in_(list(confirm_keys)),
                ErpOperation.target_external_id.in_(list(confirm_keys)),
            ),
            ErpOperation.status == "succeeded",
            ErpOperation.mirror_confirmed_at.is_(None),
        )
        db.execute(base.where(ErpOperation.kind != "cancel_order").values(mirror_confirmed_at=utcnow()))
        if cancelled_keys:  # cancelamento: só quando o espelho já mostra o pedido cancelado
            db.execute(
                base.where(
                    ErpOperation.kind == "cancel_order",
                    or_(
                        ErpOperation.external_id.in_(list(cancelled_keys)),
                        ErpOperation.target_external_id.in_(list(cancelled_keys)),
                    ),
                ).values(mirror_confirmed_at=utcnow())
            )
    db.flush()
    return persisted, unchanged, quarantined


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
