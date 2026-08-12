from datetime import datetime
from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base

class Customer(Base):
    __tablename__="customers"
    id: Mapped[int]=mapped_column(primary_key=True)
    mercos_id: Mapped[str]=mapped_column(String(80), unique=True, index=True)
    name: Mapped[str]=mapped_column(String(300), default="")
    document: Mapped[str|None]=mapped_column(String(30))
    city: Mapped[str|None]=mapped_column(String(120)); state: Mapped[str|None]=mapped_column(String(5))
    email: Mapped[str|None]=mapped_column(String(300)); phone: Mapped[str|None]=mapped_column(String(40))
    source_updated_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    raw: Mapped[dict]=mapped_column(JSON, default=dict)

class Product(Base):
    __tablename__="products"
    id: Mapped[int]=mapped_column(primary_key=True)
    mercos_id: Mapped[str]=mapped_column(String(80), unique=True, index=True)
    code: Mapped[str|None]=mapped_column(String(100), index=True); name: Mapped[str]=mapped_column(String(400), default="")
    category_id: Mapped[str|None]=mapped_column(String(80)); unit: Mapped[str|None]=mapped_column(String(30))
    list_price: Mapped[float]=mapped_column(Float, default=0); stock: Mapped[float]=mapped_column(Float, default=0)
    active: Mapped[bool]=mapped_column(Boolean, default=True); raw: Mapped[dict]=mapped_column(JSON, default=dict)

class Seller(Base):
    __tablename__="sellers"
    id: Mapped[int]=mapped_column(primary_key=True); mercos_id: Mapped[str]=mapped_column(String(80), unique=True)
    name: Mapped[str]=mapped_column(String(300), default=""); active: Mapped[bool]=mapped_column(Boolean, default=True)
    raw: Mapped[dict]=mapped_column(JSON, default=dict)

class Order(Base):
    __tablename__="orders"
    id: Mapped[int]=mapped_column(primary_key=True); mercos_id: Mapped[str]=mapped_column(String(80), unique=True, index=True)
    number: Mapped[str]=mapped_column(String(100), index=True); customer_mercos_id: Mapped[str|None]=mapped_column(String(80), index=True)
    seller_mercos_id: Mapped[str|None]=mapped_column(String(80), index=True); status: Mapped[str]=mapped_column(String(50), index=True)
    issued_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), index=True); total: Mapped[float]=mapped_column(Float, default=0)
    discount: Mapped[float]=mapped_column(Float, default=0); source_updated_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    raw: Mapped[dict]=mapped_column(JSON, default=dict)

class OrderItem(Base):
    __tablename__="order_items"; __table_args__=(UniqueConstraint("order_mercos_id","position"),)
    id: Mapped[int]=mapped_column(primary_key=True); order_mercos_id: Mapped[str]=mapped_column(String(80), index=True)
    position: Mapped[int]=mapped_column(Integer); product_mercos_id: Mapped[str|None]=mapped_column(String(80), index=True)
    code: Mapped[str|None]=mapped_column(String(100)); name: Mapped[str]=mapped_column(String(400), default="")
    quantity: Mapped[float]=mapped_column(Float, default=0); unit_price: Mapped[float]=mapped_column(Float, default=0)
    discount: Mapped[float]=mapped_column(Float, default=0); total: Mapped[float]=mapped_column(Float, default=0)
    raw: Mapped[dict]=mapped_column(JSON, default=dict)

class SyncState(Base):
    __tablename__="sync_states"
    resource: Mapped[str]=mapped_column(String(50), primary_key=True); cursor: Mapped[str|None]=mapped_column(Text)
    last_success_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True)); status: Mapped[str]=mapped_column(String(30), default="never")
    records: Mapped[int]=mapped_column(Integer, default=0); error: Mapped[str|None]=mapped_column(Text)

