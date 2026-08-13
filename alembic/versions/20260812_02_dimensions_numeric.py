"""Add analytical dimensions and migrate monetary columns to Numeric.

Revision ID: 20260812_02
Revises: 20260812_01
Create Date: 2026-08-12
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260812_02"
down_revision: str | None = "20260812_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DIMENSIONS = {
    "customer_segments": False,
    "order_types": False,
    "payment_conditions": False,
    "price_tables": False,
    "carriers": False,
    "commercial_policies": False,
    "categories": True,
}


def _inspector():
    return sa.inspect(op.get_bind())


def _tables() -> set[str]:
    return set(_inspector().get_table_names())


def _columns(table: str) -> set[str]:
    return {column["name"] for column in _inspector().get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {
        index["name"]
        for index in _inspector().get_indexes(table)
        if index.get("name")
    }


def _add_columns(table: str, definitions: list[sa.Column]) -> None:
    existing = _columns(table)
    for column in definitions:
        if column.name not in existing:
            op.add_column(table, column)


def _create_dimension(table: str, with_parent: bool) -> None:
    columns = [
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("mercos_id", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
    ]
    if with_parent:
        columns.append(sa.Column("parent_mercos_id", sa.String(length=80)))
    columns.extend(
        [
            sa.Column(
                "active",
                sa.Boolean(),
                server_default=sa.true(),
                nullable=False,
            ),
            sa.Column("source_updated_at", sa.DateTime(timezone=True)),
            sa.Column("raw", sa.JSON(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("mercos_id", name=f"uq_{table}_mercos_id"),
        ]
    )
    op.create_table(table, *columns)
    op.create_index(f"ix_{table}_mercos_id", table, ["mercos_id"])
    if with_parent:
        op.create_index(
            f"ix_{table}_parent_mercos_id",
            table,
            ["parent_mercos_id"],
        )


def _to_numeric(table: str, column: str, precision: int, scale: int) -> None:
    if table not in _tables() or column not in _columns(table):
        return
    target = sa.Numeric(precision, scale)
    if op.get_bind().dialect.name == "postgresql":
        op.alter_column(
            table,
            column,
            type_=target,
            postgresql_using=f"{column}::numeric({precision},{scale})",
        )
    else:
        with op.batch_alter_table(table) as batch:
            batch.alter_column(column, type_=target)


def _quote_identifier(value: str) -> str:
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


def _drop_dependent_views() -> list[dict]:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return []
    rows = bind.execute(
        sa.text(
            """
            WITH RECURSIVE dependencies AS (
                SELECT DISTINCT
                    rewrite.ev_class AS view_oid,
                    dependency.refobjid AS referenced_oid
                FROM pg_rewrite AS rewrite
                JOIN pg_depend AS dependency
                  ON dependency.objid = rewrite.oid
                JOIN pg_class AS view_class
                  ON view_class.oid = rewrite.ev_class
                WHERE view_class.relkind = 'v'
                  AND dependency.deptype = 'n'
            ),
            roots AS (
                SELECT DISTINCT dependencies.view_oid, 0 AS depth
                FROM dependencies
                JOIN pg_class AS referenced
                  ON referenced.oid = dependencies.referenced_oid
                JOIN pg_namespace AS referenced_schema
                  ON referenced_schema.oid = referenced.relnamespace
                WHERE referenced_schema.nspname = current_schema()
                  AND referenced.relname IN ('products', 'orders', 'order_items')
            ),
            affected AS (
                SELECT view_oid, depth FROM roots
                UNION ALL
                SELECT dependencies.view_oid, affected.depth + 1
                FROM dependencies
                JOIN affected
                  ON dependencies.referenced_oid = affected.view_oid
            )
            SELECT
                view_schema.nspname AS schema_name,
                view_class.relname AS view_name,
                pg_get_viewdef(view_class.oid, true) AS definition,
                pg_get_userbyid(view_class.relowner) AS owner_name,
                view_class.reloptions AS options,
                max(affected.depth) AS depth
            FROM affected
            JOIN pg_class AS view_class
              ON view_class.oid = affected.view_oid
            JOIN pg_namespace AS view_schema
              ON view_schema.oid = view_class.relnamespace
            WHERE view_schema.nspname NOT IN ('pg_catalog', 'information_schema')
            GROUP BY
                view_class.oid,
                view_schema.nspname,
                view_class.relname,
                view_class.relowner,
                view_class.reloptions
            ORDER BY depth DESC, view_schema.nspname, view_class.relname
            """
        )
    ).mappings().all()
    views = [dict(row) for row in rows]
    for view in views:
        qualified_name = (
            f"{_quote_identifier(view['schema_name'])}."
            f"{_quote_identifier(view['view_name'])}"
        )
        op.execute(f"DROP VIEW {qualified_name}")
    return views


def _restore_dependent_views(views: list[dict]) -> None:
    for view in sorted(
        views,
        key=lambda item: (
            item["depth"],
            item["schema_name"],
            item["view_name"],
        ),
    ):
        qualified_name = (
            f"{_quote_identifier(view['schema_name'])}."
            f"{_quote_identifier(view['view_name'])}"
        )
        options = view["options"] or []
        options_sql = f" WITH ({', '.join(options)})" if options else ""
        op.execute(
            f"CREATE VIEW {qualified_name}{options_sql} AS {view['definition']}"
        )
        op.execute(
            f"ALTER VIEW {qualified_name} OWNER TO "
            f"{_quote_identifier(view['owner_name'])}"
        )


def upgrade() -> None:
    tables = _tables()
    for table, with_parent in DIMENSIONS.items():
        if table not in tables:
            _create_dimension(table, with_parent)

    if "product_prices" not in tables:
        op.create_table(
            "product_prices",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("product_mercos_id", sa.String(length=80), nullable=False),
            sa.Column("price_table_mercos_id", sa.String(length=80), nullable=False),
            sa.Column(
                "price",
                sa.Numeric(18, 2),
                server_default="0",
                nullable=False,
            ),
            sa.Column("source_updated_at", sa.DateTime(timezone=True)),
            sa.Column("raw", sa.JSON(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "product_mercos_id",
                "price_table_mercos_id",
                name="uq_product_prices_product_table",
            ),
        )
        op.create_index(
            "ix_product_prices_product_mercos_id",
            "product_prices",
            ["product_mercos_id"],
        )
        op.create_index(
            "ix_product_prices_price_table_mercos_id",
            "product_prices",
            ["price_table_mercos_id"],
        )

    tables = _tables()
    if "customers" in tables:
        _add_columns(
            "customers",
            [
                sa.Column("segment_mercos_id", sa.String(length=80)),
                sa.Column("created_at_source", sa.DateTime(timezone=True)),
                sa.Column(
                    "active",
                    sa.Boolean(),
                    server_default=sa.true(),
                    nullable=False,
                ),
            ],
        )
        if "ix_customers_segment_mercos_id" not in _indexes("customers"):
            op.create_index(
                "ix_customers_segment_mercos_id",
                "customers",
                ["segment_mercos_id"],
            )

    if "products" in tables:
        _add_columns(
            "products",
            [
                sa.Column("category_mercos_id", sa.String(length=80)),
                sa.Column("minimum_price", sa.Numeric(18, 2)),
                sa.Column("source_updated_at", sa.DateTime(timezone=True)),
                sa.Column("created_at_source", sa.DateTime(timezone=True)),
            ],
        )
        if "ix_products_category_mercos_id" not in _indexes("products"):
            op.create_index(
                "ix_products_category_mercos_id",
                "products",
                ["category_mercos_id"],
            )

    if "orders" in tables:
        order_columns = [
            sa.Column("order_type_mercos_id", sa.String(length=80)),
            sa.Column("payment_condition_mercos_id", sa.String(length=80)),
            sa.Column("price_table_mercos_id", sa.String(length=80)),
            sa.Column("carrier_mercos_id", sa.String(length=80)),
            sa.Column("commercial_policy_mercos_id", sa.String(length=80)),
            sa.Column("gross_total", sa.Numeric(18, 2)),
            sa.Column("net_total", sa.Numeric(18, 2)),
            sa.Column("discount_value", sa.Numeric(18, 2)),
            sa.Column("discount_percent", sa.Numeric(9, 4)),
            sa.Column("item_count", sa.Integer()),
            sa.Column("sku_count", sa.Integer()),
            sa.Column("source_created_at", sa.DateTime(timezone=True)),
        ]
        _add_columns("orders", order_columns)
        for column in (
            "order_type_mercos_id",
            "payment_condition_mercos_id",
            "price_table_mercos_id",
            "carrier_mercos_id",
            "commercial_policy_mercos_id",
        ):
            name = f"ix_orders_{column}"
            if name not in _indexes("orders"):
                op.create_index(name, "orders", [column])

    dependent_views = _drop_dependent_views()
    for table, column, precision, scale in (
        ("products", "list_price", 18, 2),
        ("products", "stock", 18, 4),
        ("orders", "total", 18, 2),
        ("orders", "discount", 18, 2),
        ("order_items", "quantity", 18, 4),
        ("order_items", "unit_price", 18, 2),
        ("order_items", "discount", 18, 2),
        ("order_items", "total", 18, 2),
    ):
        _to_numeric(table, column, precision, scale)
    _restore_dependent_views(dependent_views)


def downgrade() -> None:
    for table, column in (
        ("products", "list_price"),
        ("products", "stock"),
        ("orders", "total"),
        ("orders", "discount"),
        ("order_items", "quantity"),
        ("order_items", "unit_price"),
        ("order_items", "discount"),
        ("order_items", "total"),
    ):
        if table in _tables() and column in _columns(table):
            if op.get_bind().dialect.name == "postgresql":
                op.alter_column(
                    table,
                    column,
                    type_=sa.Float(),
                    postgresql_using=f"{column}::double precision",
                )
            else:
                with op.batch_alter_table(table) as batch:
                    batch.alter_column(column, type_=sa.Float())

    drop_columns = {
        "customers": ["active", "created_at_source", "segment_mercos_id"],
        "products": [
            "created_at_source",
            "source_updated_at",
            "minimum_price",
            "category_mercos_id",
        ],
        "orders": [
            "source_created_at",
            "sku_count",
            "item_count",
            "discount_percent",
            "discount_value",
            "net_total",
            "gross_total",
            "commercial_policy_mercos_id",
            "carrier_mercos_id",
            "price_table_mercos_id",
            "payment_condition_mercos_id",
            "order_type_mercos_id",
        ],
    }
    for table, columns in drop_columns.items():
        if table not in _tables():
            continue
        existing = _columns(table)
        with op.batch_alter_table(table) as batch:
            for column in columns:
                if column in existing:
                    batch.drop_column(column)

    if "product_prices" in _tables():
        op.drop_table("product_prices")
    for table in reversed(tuple(DIMENSIONS)):
        if table in _tables():
            op.drop_table(table)
