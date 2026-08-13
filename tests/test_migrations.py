from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def test_phase2_migration_upgrades_legacy_order_items(tmp_path):
    database_path = tmp_path / "legacy.db"
    url = f"sqlite:///{database_path.as_posix()}"
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE order_items (
                id INTEGER PRIMARY KEY,
                order_mercos_id VARCHAR(80) NOT NULL,
                position INTEGER NOT NULL,
                UNIQUE (order_mercos_id, position)
            )
            """
        )

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")

    schema = inspect(create_engine(url))
    assert "sync_runs" in schema.get_table_names()
    assert "export_runs" in schema.get_table_names()
    assert "categories" in schema.get_table_names()
    assert "customer_segments" in schema.get_table_names()
    assert "product_prices" in schema.get_table_names()
    assert "mercos_item_id" in {
        column["name"] for column in schema.get_columns("order_items")
    }
    unique_columns = {
        tuple(constraint["column_names"])
        for constraint in schema.get_unique_constraints("order_items")
    }
    assert ("order_mercos_id", "mercos_item_id") in unique_columns

    command.downgrade(config, "base")
    rolled_back = inspect(create_engine(url))
    assert "sync_runs" not in rolled_back.get_table_names()
    assert "export_runs" not in rolled_back.get_table_names()
    assert "categories" not in rolled_back.get_table_names()
    assert "mercos_item_id" not in {
        column["name"] for column in rolled_back.get_columns("order_items")
    }
