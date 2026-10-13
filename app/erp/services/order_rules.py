"""Regras únicas de elegibilidade de pedido para ações locais (frete, rascunho fiscal, título).

O backend recusa com estas mesmas regras; a API de leitura as expõe como `actions` para o
frontend desabilitar o botão com o motivo. Uma só fonte evita tela e servidor divergirem."""

BLOCKS = {
    "shipping": {
        "cancelled": "Pedido cancelado não recebe cotação de frete",
        "incomplete": "Itens do pedido ainda não foram sincronizados; a cotação depende da lista completa",
    },
    "invoice": {
        "cancelled": "Pedido cancelado não recebe rascunho fiscal",
        "incomplete": "Itens do pedido ainda não foram sincronizados; o rascunho depende da lista completa",
        "total": "O pedido não tem valor total conhecido",
    },
    "receivable": {
        "cancelled": "Pedido cancelado não gera título",
        "customer": "O pedido não tem cliente identificado",
    },
}
CODES = {
    "cancelled": "order_cancelled",
    "incomplete": "items_incomplete",
    "total": "order_total_unavailable",
    "customer": "customer_required",
}
ORDER = {
    "shipping": ("cancelled", "incomplete"),
    "invoice": ("cancelled", "incomplete", "total"),
    "receivable": ("cancelled", "customer"),
}


def _violates(order, reason: str) -> bool:
    if reason == "cancelled":
        return order.kind == "cancelled"
    if reason == "incomplete":
        return not order.items_complete
    if reason == "total":
        return order.net_total is None or order.net_total <= 0
    if reason == "customer":
        return not order.customer_external_id
    return False


def block(order, action: str) -> tuple[str, str] | None:
    """(código, mensagem) do primeiro impedimento, ou None se a ação é permitida."""
    for reason in ORDER[action]:
        if _violates(order, reason):
            return CODES[reason], BLOCKS[action][reason]
    return None


def availability(order) -> dict[str, dict]:
    out = {}
    for action in ORDER:
        blocked = block(order, action)
        out[action] = {
            "allowed": blocked is None,
            "code": blocked[0] if blocked else None,
            "reason": blocked[1] if blocked else None,
        }
    return out
