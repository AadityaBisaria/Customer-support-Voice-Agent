"""Pure domain model: value objects, entities, state machines.

Imports nothing from the application — stdlib only. Illegal states are made
unrepresentable at construction: money is integer paise with no float ops,
phone numbers validate to exactly ten digits, IDs are distinct types so
cross-wiring one kind into another is a type error, and status changes go
through declarative transition maps.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, NewType

# ---------------------------------------------------------------- typed IDs

CustomerId = NewType("CustomerId", int)
OrderId = NewType("OrderId", str)
OrderItemId = NewType("OrderItemId", int)
VariantId = NewType("VariantId", int)
ReturnId = NewType("ReturnId", str)
RefundId = NewType("RefundId", str)


# ------------------------------------------------------------ value objects


@dataclass(frozen=True, slots=True)
class Money:
    """An INR amount as integer paise. Floats never touch money."""

    paise: int

    def __post_init__(self) -> None:
        if not isinstance(self.paise, int):
            raise TypeError("Money is integer paise; got " + type(self.paise).__name__)

    @classmethod
    def rupees(cls, r: int) -> "Money":
        return cls(paise=r * 100)

    def __add__(self, other: "Money") -> "Money":
        return Money(self.paise + other.paise)

    def __sub__(self, other: "Money") -> "Money":
        return Money(self.paise - other.paise)

    def __mul__(self, n: int) -> "Money":
        if not isinstance(n, int):
            raise TypeError("Money can only be multiplied by an integer count")
        return Money(self.paise * n)

    def speak(self) -> str:
        """TTS-safe rendering: whole rupees, paise only when nonzero."""
        rupees, paise = divmod(self.paise, 100)
        if paise:
            return f"{rupees} rupees {paise} paise"
        return f"{rupees} rupees"


_PHONE_JUNK = re.compile(r"[^\d]")


@dataclass(frozen=True, slots=True)
class PhoneNumber:
    """A normalized Indian mobile number: exactly ten digits."""

    digits: str

    def __post_init__(self) -> None:
        if len(self.digits) != 10 or not self.digits.isdigit():
            raise ValueError(f"phone number must be exactly 10 digits, got {self.digits!r}")

    @classmethod
    def parse(cls, raw: str) -> "PhoneNumber":
        """Normalize free-form input: strip punctuation, +91 / leading 0."""
        digits = _PHONE_JUNK.sub("", raw)
        if len(digits) == 12 and digits.startswith("91"):
            digits = digits[2:]
        elif len(digits) == 11 and digits.startswith("0"):
            digits = digits[1:]
        return cls(digits=digits)


# ------------------------------------------------------------------- enums


class OrderStatus(StrEnum):
    PLACED = "placed"
    SHIPPED = "shipped"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class PaymentMethod(StrEnum):
    AMAZON_PAY = "amazon_pay"
    CARD = "card"
    UPI = "upi"
    NETBANKING = "netbanking"
    POD = "pod"


class PolicyType(StrEnum):
    RETURNABLE_10D = "returnable_10d"
    RETURNABLE_30D = "returnable_30d"
    REPLACEMENT_ONLY_7D = "replacement_only_7d"
    REPLACEMENT_ONLY_10D = "replacement_only_10d"
    NON_RETURNABLE = "non_returnable"


class Resolution(StrEnum):
    REFUND = "refund"
    REPLACEMENT = "replacement"


class ReturnReason(StrEnum):
    DAMAGED = "damaged"
    DEFECTIVE = "defective"
    WRONG_ITEM = "wrong_item"
    MISSING_PARTS = "missing_parts"
    NOT_NEEDED = "not_needed"
    SIZE_ISSUE = "size_issue"


# Reasons where the fault is with the shipment, not the buyer — these unlock
# refund/replacement even on replacement-only and non-returnable policies
# (KB qa-018, qa-023).
DAMAGE_CLASS_REASONS = frozenset(
    {
        ReturnReason.DAMAGED,
        ReturnReason.DEFECTIVE,
        ReturnReason.WRONG_ITEM,
        ReturnReason.MISSING_PARTS,
    }
)


class ReturnStatus(StrEnum):
    REQUESTED = "requested"
    PICKUP_SCHEDULED = "pickup_scheduled"
    PICKED_UP = "picked_up"
    COMPLETED = "completed"
    REJECTED = "rejected"


class RefundMethod(StrEnum):
    AMAZON_PAY = "amazon_pay"
    CARD = "card"
    UPI = "upi"
    NETBANKING = "netbanking"
    NEFT = "neft"
    CHEQUE = "cheque"


class RefundStatus(StrEnum):
    INITIATED = "initiated"
    PROCESSED = "processed"
    COMPLETED = "completed"


# ---------------------------------------------------------- state machines


class IllegalTransition(Exception):
    def __init__(self, current: StrEnum, to: StrEnum) -> None:
        super().__init__(f"illegal transition {current.value!r} -> {to.value!r}")
        self.current = current
        self.to = to


ORDER_TRANSITIONS: Mapping[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PLACED: frozenset({OrderStatus.SHIPPED, OrderStatus.CANCELLED}),
    OrderStatus.SHIPPED: frozenset({OrderStatus.OUT_FOR_DELIVERY}),
    OrderStatus.OUT_FOR_DELIVERY: frozenset({OrderStatus.DELIVERED}),
    OrderStatus.DELIVERED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
}

RETURN_TRANSITIONS: Mapping[ReturnStatus, frozenset[ReturnStatus]] = {
    ReturnStatus.REQUESTED: frozenset({ReturnStatus.PICKUP_SCHEDULED, ReturnStatus.REJECTED}),
    ReturnStatus.PICKUP_SCHEDULED: frozenset({ReturnStatus.PICKED_UP, ReturnStatus.REJECTED}),
    ReturnStatus.PICKED_UP: frozenset({ReturnStatus.COMPLETED}),
    ReturnStatus.COMPLETED: frozenset(),
    ReturnStatus.REJECTED: frozenset(),
}

REFUND_TRANSITIONS: Mapping[RefundStatus, frozenset[RefundStatus]] = {
    RefundStatus.INITIATED: frozenset({RefundStatus.PROCESSED}),
    RefundStatus.PROCESSED: frozenset({RefundStatus.COMPLETED}),
    RefundStatus.COMPLETED: frozenset(),
}


def advance[S: StrEnum](current: S, to: S, transitions: Mapping[S, frozenset[S]]) -> S:
    """The only sanctioned way to compute a next status."""
    if to not in transitions.get(current, frozenset()):
        raise IllegalTransition(current, to)
    return to


# ---------------------------------------------------------------- entities
# Frozen kw-only dataclasses. Rows/aggregates loaded by the store; the price
# on an order item is a snapshot of what was paid — a historical fact.


@dataclass(frozen=True, slots=True, kw_only=True)
class Customer:
    id: CustomerId
    name: str
    phone: PhoneNumber
    email: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Address:
    id: int
    customer_id: CustomerId
    label: str
    line1: str
    city: str
    pincode: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Product:
    id: int
    title: str
    price: Money
    return_policy_type: PolicyType


@dataclass(frozen=True, slots=True, kw_only=True)
class ProductVariant:
    id: VariantId
    product_id: int
    size: str | None
    color: str | None
    in_stock: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderItem:
    id: OrderItemId
    order_id: OrderId
    variant_id: VariantId
    quantity: int
    price: Money  # snapshot at purchase


@dataclass(frozen=True, slots=True, kw_only=True)
class Order:
    """The Order aggregate: order row + its items, loaded together."""

    id: OrderId
    customer_id: CustomerId
    address_id: int
    status: OrderStatus
    payment_method: PaymentMethod
    placed_at: datetime
    shipped_at: datetime | None
    delivered_at: datetime | None
    total: Money
    items: tuple[OrderItem, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class ReturnRequest:
    id: ReturnId
    order_item_id: OrderItemId
    resolution: Resolution
    reason: ReturnReason
    status: ReturnStatus
    replacement_variant_id: VariantId | None
    created_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class Refund:
    id: RefundId
    order_id: OrderId
    return_request_id: ReturnId | None
    origin: str  # 'return' | 'cancellation'
    amount: Money
    method: RefundMethod
    status: RefundStatus
    initiated_at: datetime
    expected_by: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class DomainEvent:
    seq: int
    occurred_at: datetime
    aggregate_type: str
    aggregate_id: str
    event_type: str
    payload: Mapping[str, Any]
    idempotency_key: str | None


# ------------------------------------------------------------- view models
# Speakable projections for the conversation layer.


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderSummary:
    id: OrderId
    status: OrderStatus
    payment_method: PaymentMethod
    placed_at: datetime
    delivered_at: datetime | None
    item_titles: tuple[str, ...]
    total: Money


@dataclass(frozen=True, slots=True, kw_only=True)
class ItemDetail:
    item: OrderItem
    variant: ProductVariant
    product: Product


@dataclass(frozen=True, slots=True, kw_only=True)
class RefundView:
    refund: Refund
    order_item_titles: tuple[str, ...]


# --------------------------------------------------------- mutation results
# What a SupportStore mutation hands back — also the payload persisted in the
# event log, so an idempotent replay returns exactly this.


@dataclass(frozen=True, slots=True, kw_only=True)
class RefundInfo:
    refund_id: RefundId
    amount: Money
    method: RefundMethod
    expected_by: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class CancelResult:
    order_id: OrderId
    refund: RefundInfo | None  # None for Pay-on-Delivery cancels


@dataclass(frozen=True, slots=True, kw_only=True)
class ReturnResult:
    return_id: ReturnId
    order_item_id: OrderItemId
    resolution: Resolution
    refund: RefundInfo | None  # None for replacements
    pickup_by: datetime
