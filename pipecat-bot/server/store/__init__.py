"""The data layer: pure domain model, business policy, and a swappable store.

Layering (dependencies point inward — enforced by a lint test):

    domain.py   value objects, entities, state machines   (stdlib only)
    policy.py   business rules mirroring the KB corpus    (imports domain)
    ports.py    SupportStore Protocol — the production seam
    sqlite.py   persistence implementation                (imports domain, ports)
    seed.py     deterministic demo fixtures
    clock.py    injected time (no naked datetime.now())

Conversation code (`flows/`) imports ONLY domain/policy/ports — never sqlite —
so swapping SQLite for a real order-management API means re-implementing
`SupportStore` and touching zero flow code.
"""

from .clock import Clock, FixedClock, IstClock
from .domain import (
    Address,
    CancelResult,
    Customer,
    CustomerId,
    DomainEvent,
    IllegalTransition,
    ItemDetail,
    Money,
    Order,
    OrderId,
    OrderItem,
    OrderItemId,
    OrderStatus,
    OrderSummary,
    PaymentMethod,
    PhoneNumber,
    PolicyType,
    Product,
    ProductVariant,
    RefundId,
    RefundInfo,
    RefundMethod,
    RefundStatus,
    RefundView,
    Resolution,
    ReturnId,
    ReturnReason,
    ReturnRequest,
    ReturnResult,
    ReturnStatus,
    VariantId,
    advance,
)
from .policy import (
    PolicyError,
    check_cancellable,
    refund_expectation,
    return_window,
    valid_resolutions,
)
from .ports import SupportStore

# Deliberately NOT re-exported: store.sqlite. The persistence implementation
# is imported only where it's constructed (bot.py, seed tooling, its tests) —
# `from store.sqlite import SqliteSupportStore` — so conversation code cannot
# accidentally depend on it.

__all__ = [
    "Address",
    "CancelResult",
    "Clock",
    "Customer",
    "CustomerId",
    "DomainEvent",
    "FixedClock",
    "IllegalTransition",
    "IstClock",
    "ItemDetail",
    "Money",
    "Order",
    "OrderId",
    "OrderItem",
    "OrderItemId",
    "OrderStatus",
    "OrderSummary",
    "PaymentMethod",
    "PhoneNumber",
    "PolicyError",
    "PolicyType",
    "Product",
    "ProductVariant",
    "RefundId",
    "RefundInfo",
    "RefundMethod",
    "RefundStatus",
    "RefundView",
    "Resolution",
    "ReturnId",
    "ReturnReason",
    "ReturnRequest",
    "ReturnResult",
    "ReturnStatus",
    "SupportStore",
    "VariantId",
    "advance",
    "check_cancellable",
    "refund_expectation",
    "return_window",
    "valid_resolutions",
]
