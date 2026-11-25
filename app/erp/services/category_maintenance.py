"""Manutenção local de categorias e subcategorias.

As categorias espelhadas do Mercos entram na mesma lista. A escrita fica neste
ERP: o Adaptor não tem método confirmado para gravar categoria no Mercos.
"""

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.erp.audit import audit
from app.erp.auth import ErpUser
from app.erp.common import http_error, utcnow
from app.erp.models.commercial import ErpCategory
from app.erp.schemas.commands import CategoryCreate, CategoryPatch


def list_categories(db: Session, connection_id: str) -> list[ErpCategory]:
    return list(db.scalars(
        select(ErpCategory)
        .where(ErpCategory.connection_id == connection_id, ErpCategory.source_deleted.is_(False))
        .order_by(ErpCategory.name)
    ))


def get_category(db: Session, connection_id: str, external_id: str) -> ErpCategory:
    row = db.scalar(
        select(ErpCategory).where(
            ErpCategory.connection_id == connection_id,
            ErpCategory.external_id == external_id,
        )
    )
    if row is None or row.source_deleted:
        raise http_error(404, "not_found", "Categoria não encontrada")
    return row


def _parent(db: Session, connection_id: str, parent_id: str | None, self_id: str | None) -> None:
    if not parent_id:
        return
    seen: set[str] = set()
    current: str | None = parent_id
    while current:
        if current == self_id or current in seen:
            raise http_error(422, "invalid_parent", "A subcategoria não pode ficar abaixo dela mesma")
        seen.add(current)
        row = db.scalar(
            select(ErpCategory).where(
                ErpCategory.connection_id == connection_id,
                ErpCategory.external_id == current,
                ErpCategory.source_deleted.is_(False),
            )
        )
        if row is None:
            raise http_error(422, "unknown_parent", "Categoria pai não encontrada")
        current = row.parent_external_id


def _touch(row: ErpCategory) -> None:
    row.version = (row.version or 1) + 1
    row.source_updated_at = utcnow()
    row.captured_at = utcnow()
    row.fingerprint = None


def create_category(db: Session, user: ErpUser, connection_id: str, body: CategoryCreate) -> ErpCategory:
    data = body.model_dump(exclude_unset=True)
    parent_id = data.get("parentId")
    _parent(db, connection_id, parent_id, None)
    row = ErpCategory(
        connection_id=connection_id,
        external_id=f"local-cat-{uuid4().hex[:16]}",
        name=data["name"],
        parent_external_id=parent_id,
        active=True if data.get("active") is None else data["active"],
        source_deleted=False,
        version=1,
        source_updated_at=utcnow(),
        captured_at=utcnow(),
    )
    db.add(row)
    db.flush()
    audit(
        db, operator=user.username, action="category.create", connection_id=connection_id,
        resource="categories", resource_id=row.external_id, result="ok", detail={"name": row.name},
    )
    return row


def update_category(
    db: Session, user: ErpUser, connection_id: str, external_id: str, body: CategoryPatch,
) -> ErpCategory:
    row = get_category(db, connection_id, external_id)
    data = body.model_dump(exclude_unset=True)
    expected = data.pop("expectedVersion")
    if row.version != expected:
        raise http_error(
            409, "version_mismatch", "A versão local mudou; recarregue antes de editar",
            currentVersion=row.version,
        )
    if not data:
        raise http_error(422, "empty_patch", "Nenhum campo para alterar")
    if "parentId" in data:
        _parent(db, connection_id, data["parentId"], row.external_id)
        row.parent_external_id = data["parentId"]
    if "name" in data:
        row.name = data["name"]
    if "active" in data:
        row.active = data["active"]
    _touch(row)
    db.add(row)
    audit(
        db, operator=user.username, action="category.update", connection_id=connection_id,
        resource="categories", resource_id=row.external_id, result="ok",
        detail={"fields": sorted(data)},
    )
    return row


def delete_category(db: Session, user: ErpUser, connection_id: str, external_id: str) -> ErpCategory:
    row = get_category(db, connection_id, external_id)
    child = db.scalar(
        select(ErpCategory.id).where(
            ErpCategory.connection_id == connection_id,
            ErpCategory.parent_external_id == external_id,
            ErpCategory.source_deleted.is_(False),
        )
    )
    if child is not None:
        raise http_error(409, "category_has_children", "Exclua as subcategorias antes desta categoria")
    row.source_deleted = True
    row.active = False
    _touch(row)
    db.add(row)
    audit(
        db, operator=user.username, action="category.delete", connection_id=connection_id,
        resource="categories", resource_id=row.external_id, result="ok",
    )
    return row
