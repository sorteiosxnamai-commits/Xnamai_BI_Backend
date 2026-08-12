from datetime import datetime, timezone
from sqlalchemy import delete, select
from app.adaptor import adaptor
from app.database import SessionLocal
from app.models import Customer, Order, OrderItem, Product, Seller, SyncState

def dt(value):
    if not value:return None
    try:return datetime.fromisoformat(str(value).replace("Z","+00:00"))
    except ValueError:return None
def f(value):
    try:return float(value or 0)
    except (ValueError,TypeError):return 0

async def sync_resource(resource:str, full=False):
    with SessionLocal() as db:
        state=db.get(SyncState,resource) or SyncState(resource=resource)
        cursor=None if full else state.cursor
        state.status="running"; state.error=None; db.add(state); db.commit()
        try:
            result=await adaptor.list(resource,cursor); rows=result.get("data",[])
            for row in rows:
                mid=str(row.get("id"))
                if resource=="customers":
                    obj=db.scalar(select(Customer).where(Customer.mercos_id==mid)) or Customer(mercos_id=mid)
                    obj.name=row.get("nome") or row.get("razao_social") or "Sem nome"; obj.document=row.get("cnpj") or row.get("cpf")
                    obj.city=row.get("cidade"); obj.state=row.get("estado"); obj.email=(row.get("emails") or [{}])[0].get("email") if isinstance((row.get("emails") or [{}])[0],dict) else None
                    obj.phone=row.get("celular") or row.get("telefone"); obj.source_updated_at=dt(row.get("ultima_alteracao")); obj.raw=row; db.add(obj)
                elif resource=="products":
                    obj=db.scalar(select(Product).where(Product.mercos_id==mid)) or Product(mercos_id=mid)
                    obj.code=str(row.get("codigo") or ""); obj.name=row.get("nome") or "Sem nome"; obj.category_id=str(row.get("categoria_id") or "") or None
                    obj.unit=row.get("unidade"); obj.list_price=f(row.get("preco_tabela")); obj.stock=f(row.get("saldo_estoque")); obj.active=bool(row.get("ativo",True)); obj.raw=row; db.add(obj)
                elif resource=="users":
                    obj=db.scalar(select(Seller).where(Seller.mercos_id==mid)) or Seller(mercos_id=mid)
                    obj.name=row.get("nome") or row.get("email") or "Sem nome"; obj.active=bool(row.get("ativo",True)); obj.raw=row; db.add(obj)
                elif resource=="orders":
                    obj=db.scalar(select(Order).where(Order.mercos_id==mid)) or Order(mercos_id=mid,number=mid,status="unknown")
                    obj.number=str(row.get("numero") or mid); obj.customer_mercos_id=str(row.get("cliente_id") or "") or None; obj.seller_mercos_id=str(row.get("usuario_id") or row.get("vendedor_id") or "") or None
                    obj.status=str(row.get("status") or row.get("situacao") or "unknown"); obj.issued_at=dt(row.get("data_emissao") or row.get("data_criacao")); obj.total=f(row.get("total")); obj.discount=f(row.get("desconto")); obj.source_updated_at=dt(row.get("ultima_alteracao")); obj.raw=row; db.add(obj)
                    db.flush(); db.execute(delete(OrderItem).where(OrderItem.order_mercos_id==mid))
                    for pos,item in enumerate(row.get("itens") or row.get("items") or []):
                        q=f(item.get("quantidade")); unit=f(item.get("preco_unitario") or item.get("preco")); total=f(item.get("total") or q*unit)
                        db.add(OrderItem(order_mercos_id=mid,position=pos,product_mercos_id=str(item.get("produto_id") or "") or None,code=str(item.get("codigo") or ""),name=item.get("nome") or item.get("descricao") or "Produto",quantity=q,unit_price=unit,discount=f(item.get("desconto")),total=total,raw=item))
            state.cursor=result.get("nextCursor") or state.cursor; state.last_success_at=datetime.now(timezone.utc); state.status="success"; state.records=len(rows); db.add(state); db.commit()
            return {"resource":resource,"records":len(rows),"cursor":state.cursor}
        except Exception as exc:
            db.rollback(); state=db.get(SyncState,resource) or SyncState(resource=resource); state.status="error"; state.error=str(exc)[:1000]; db.add(state); db.commit(); raise

async def sync_all(full=False):
    return [await sync_resource(r,full) for r in ("customers","products","users","orders")]

