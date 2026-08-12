CREATE INDEX IF NOT EXISTS ix_orders_date_status ON orders (issued_at, status);
CREATE INDEX IF NOT EXISTS ix_items_product ON order_items (product_mercos_id);
CREATE INDEX IF NOT EXISTS ix_orders_customer ON orders (customer_mercos_id);
CREATE INDEX IF NOT EXISTS ix_orders_seller ON orders (seller_mercos_id);

