"""The SupportStore Protocol — the production seam.

The conversation layer depends on THIS, never on concrete database adapters.
All reads and mutations pass through this asynchronous boundary.

Mutations are never registered directly as LLM tools; only deterministic
confirmation gates (e.g., flows/confirm.py) execute them with idempotency keys.
"""

from collections.abc import Collection
from datetime import datetime
from typing import Protocol, runtime_checkable

from .domain import (
    CancelResult,
    Customer,
    CustomerAccount,
    CustomerId,
    Delivery,
    DeliveryId,
    DeliverySlotPreference,
    DisputeId,
    DisputeTicket,
    DomainEvent,
    ItemDetail,
    Order,
    OrderFeedback,
    OrderId,
    OrderItemId,
    OrderStatus,
    OrderSummary,
    PaymentCollectionMode,
    PhoneNumber,
    ProductVariant,
    RefundDestination,
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

    async def account_for_customer(
        self, customer_id: CustomerId
    ) -> CustomerAccount | None: ...

    async def orders_for_customer(
        self, customer_id: CustomerId, *, statuses: Collection[OrderStatus] | None = None
    ) -> list[OrderSummary]: ...

    async def order_with_items(self, order_id: OrderId) -> Order: ...

    async def items_for_order(self, order_id: OrderId) -> list[ItemDetail]: ...

    async def delivery_for_order(self, order_id: OrderId) -> Delivery | None: ...

    async def variants_for_product(self, product_id: int) -> list[ProductVariant]: ...

    async def return_requests_for_item(
        self, order_item_id: OrderItemId
    ) -> list[ReturnRequest]: ...

    async def refunds_for_customer(self, customer_id: CustomerId) -> list[RefundView]: ...

    async def variant_in_stock(self, variant_id: VariantId) -> bool: ...

    async def events_for_aggregate(
        self, aggregate_type: str, aggregate_id: str
    ) -> list[DomainEvent]: ...

    # -------------------------------------------- mutations (gate-only)
    async def cancel_order(
        self, *, order_id: OrderId, idempotency_key: str
    ) -> CancelResult: ...

    async def create_return(
        self,
        *,
        order_item_id: OrderItemId,
        reason: ReturnReason,
        refund_destination: RefundDestination | None,
        idempotency_key: str,
    ) -> ReturnResult: ...

    async def create_replacement(
        self,
        *,
        order_item_id: OrderItemId,
        reason: ReturnReason,
        idempotency_key: str,
    ) -> ReturnResult: ...

    async def create_exchange(
        self,
        *,
        order_item_id: OrderItemId,
        new_variant_id: VariantId,
        reason: ReturnReason,
        idempotency_key: str,
    ) -> ReturnResult: ...

    async def reschedule_delivery(
        self,
        *,
        delivery_id: DeliveryId,
        new_date: datetime,
        slot: DeliverySlotPreference | None,
        instructions: str | None,
        idempotency_key: str,
    ) -> Delivery: ...

    async def update_payment_collection_mode(
        self,
        *,
        order_id: OrderId,
        mode: PaymentCollectionMode,
        idempotency_key: str,
    ) -> Order: ...

    async def submit_dispute_ticket(
        self, *, ticket: DisputeTicket, idempotency_key: str
    ) -> DisputeId: ...

    async def record_order_feedback(
        self, *, feedback: OrderFeedback, idempotency_key: str
    ) -> None: ...