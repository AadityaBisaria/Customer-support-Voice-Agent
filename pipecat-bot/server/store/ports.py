"""The SupportStore Protocol — the production seam.

The conversation layer depends on THIS, never on sqlite.py. A real deployment
implements the same Protocol over order-management APIs; flow code doesn't
change. All methods are async (the SQLite implementation wraps its sync core
in asyncio.to_thread).

The three mutations are never registered as LLM tools; the only caller is
the confirm gate in flows/confirm.py, after the deterministic yes.
"""

from collections.abc import Collection
from typing import Protocol, runtime_checkable

from .domain import (
    CancelResult,
    Customer,
    CustomerId,
    DomainEvent,
    ItemDetail,
    Order,
    OrderId,
    OrderItemId,
    OrderStatus,
    OrderSummary,
    PhoneNumber,
    RefundMethod,
    RefundView,
    ReturnReason,
    ReturnRequest,
    ReturnResult,
    VariantId,
)


@runtime_checkable
class SupportStore(Protocol):
    # ------------------------------------------------------------- reads
    async def customer_by_phone(self, phone: PhoneNumber) -> Customer | None: ...

    async def orders_for_customer(
        self, customer_id: CustomerId, *, statuses: Collection[OrderStatus] | None = None
    ) -> list[OrderSummary]: ...

    async def order_with_items(self, order_id: OrderId) -> Order: ...

    async def items_for_order(self, order_id: OrderId) -> list[ItemDetail]: ...

    async def return_requests_for_item(self, order_item_id: OrderItemId) -> list[ReturnRequest]: ...

    async def refunds_for_customer(self, customer_id: CustomerId) -> list[RefundView]: ...

    async def variant_in_stock(self, variant_id: VariantId) -> bool: ...

    async def events_for_aggregate(
        self, aggregate_type: str, aggregate_id: str
    ) -> list[DomainEvent]: ...

    # -------------------------------------------- mutations (gate-only)
    async def cancel_order(self, *, order_id: OrderId, idempotency_key: str) -> CancelResult: ...

    async def create_return(
        self,
        *,
        order_item_id: OrderItemId,
        reason: ReturnReason,
        refund_destination: RefundMethod | None,
        idempotency_key: str,
    ) -> ReturnResult: ...

    async def create_replacement(
        self, *, order_item_id: OrderItemId, reason: ReturnReason, idempotency_key: str
    ) -> ReturnResult: ...
