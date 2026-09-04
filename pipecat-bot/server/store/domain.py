"""Pure domain model: value objects, entities, state machines.

Imports canonical schemas from models.py and defines state transition graphs
and guard logic. Illegal states are made unrepresentable at construction, and
status changes go through declarative transition maps.
"""

from collections.abc import Mapping
from enum import StrEnum
# ------------------------------------------------------------ Canonical Models
from schemas.models import (
    Address,
    CancelResult,
    Customer,
    CustomerAccount,
    CustomerAccountStatus,
    CustomerId,
    Delivery,
    DeliveryId,
    DeliverySlotPreference,
    DeliveryStatus,
    DisputeId,
    DisputeStatus,
    DisputeTicket,
    DisputeType,
    DomainEvent,
    FeedbackTag,
    FeedbackTargetType,
    ItemDetail,
    Money,
    Order,
    OrderFeedback,
    OrderId,
    OrderItem,
    OrderItemId,
    OrderItemPolicySnapshot,
    OrderItemStatus,
    OrderStatus,
    OrderSummary,
    PaymentCollectionMode,
    PaymentMethod,
    PaymentStatus,
    PhoneNumber,
    Product,
    ProductCategory,
    ProductVariant,
    Refund,
    RefundDestination,
    RefundId,
    RefundInfo,
    RefundMethod,
    RefundStatus,
    RefundView,
    Resolution,
    ReturnId,
    ReturnPolicyType,
    ReturnReason,
    ReturnRequest,
    ReturnRequestStatus,
    ReturnResult,
    VariantId,
    VerifiedChannel,
)

# Backward-compatibility aliases for existing pipeline imports
PolicyType = ReturnPolicyType
ReturnStatus = ReturnRequestStatus


# -------------------------------------------------- Damage Class Classification
# Reasons where the fault is with the shipment, not the buyer. These unlock
# refund/replacement even on replacement-only and non-returnable policies.
DAMAGE_CLASS_REASONS: frozenset[ReturnReason] = frozenset(
    {
        ReturnReason.DAMAGED,
        ReturnReason.DEFECTIVE,
        ReturnReason.WRONG_ITEM,
        ReturnReason.MISSING_PARTS,
    }
)


# ---------------------------------------------------------- State Machine Logic
class IllegalTransition(Exception):
    def __init__(self, current: StrEnum, to: StrEnum) -> None:
        super().__init__(f"illegal transition {current.value!r} -> {to.value!r}")
        self.current = current
        self.to = to


ORDER_TRANSITIONS: Mapping[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PLACED: frozenset(
        {OrderStatus.CONFIRMED, OrderStatus.SHIPPED, OrderStatus.CANCELLED}
    ),
    OrderStatus.CONFIRMED: frozenset(
        {OrderStatus.SHIPPED, OrderStatus.CANCELLED}
    ),
    OrderStatus.SHIPPED: frozenset({OrderStatus.OUT_FOR_DELIVERY}),
    OrderStatus.OUT_FOR_DELIVERY: frozenset({OrderStatus.DELIVERED}),
    OrderStatus.DELIVERED: frozenset({OrderStatus.RETURNED}),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.RETURNED: frozenset(),
}

RETURN_TRANSITIONS: Mapping[ReturnRequestStatus, frozenset[ReturnRequestStatus]] = {
    ReturnRequestStatus.REQUESTED: frozenset(
        {ReturnRequestStatus.PICKUP_SCHEDULED, ReturnRequestStatus.REJECTED}
    ),
    ReturnRequestStatus.INITIATED: frozenset(
        {ReturnRequestStatus.PICKUP_SCHEDULED, ReturnRequestStatus.REJECTED}
    ),
    ReturnRequestStatus.PICKUP_SCHEDULED: frozenset(
        {ReturnRequestStatus.PICKED_UP, ReturnRequestStatus.REJECTED}
    ),
    ReturnRequestStatus.PICKED_UP: frozenset(
        {ReturnRequestStatus.INSPECTED, ReturnRequestStatus.COMPLETED}
    ),
    ReturnRequestStatus.INSPECTED: frozenset(
        {ReturnRequestStatus.COMPLETED, ReturnRequestStatus.REJECTED}
    ),
    ReturnRequestStatus.COMPLETED: frozenset(),
    ReturnRequestStatus.REJECTED: frozenset(),
}

REFUND_TRANSITIONS: Mapping[RefundStatus, frozenset[RefundStatus]] = {
    RefundStatus.INITIATED: frozenset(
        {RefundStatus.PROCESSING, RefundStatus.FAILED}
    ),
    RefundStatus.PROCESSING: frozenset(
        {RefundStatus.PROCESSED, RefundStatus.COMPLETED, RefundStatus.FAILED}
    ),
    RefundStatus.PROCESSED: frozenset({RefundStatus.COMPLETED}),
    RefundStatus.COMPLETED: frozenset(),
    RefundStatus.FAILED: frozenset(),
}

DELIVERY_TRANSITIONS: Mapping[DeliveryStatus, frozenset[DeliveryStatus]] = {
    DeliveryStatus.MANIFESTED: frozenset({DeliveryStatus.IN_TRANSIT}),
    DeliveryStatus.IN_TRANSIT: frozenset(
        {DeliveryStatus.OUT_FOR_DELIVERY, DeliveryStatus.RETURNED_TO_ORIGIN}
    ),
    DeliveryStatus.OUT_FOR_DELIVERY: frozenset(
        {DeliveryStatus.DELIVERED, DeliveryStatus.FAILED_ATTEMPT}
    ),
    DeliveryStatus.FAILED_ATTEMPT: frozenset(
        {DeliveryStatus.OUT_FOR_DELIVERY, DeliveryStatus.RETURNED_TO_ORIGIN}
    ),
    DeliveryStatus.DELIVERED: frozenset(),
    DeliveryStatus.RETURNED_TO_ORIGIN: frozenset(),
}

DISPUTE_TRANSITIONS: Mapping[DisputeStatus, frozenset[DisputeStatus]] = {
    DisputeStatus.OPEN: frozenset(
        {
            DisputeStatus.CARRIER_INVESTIGATION,
            DisputeStatus.RESOLVED_REFUND,
            DisputeStatus.REJECTED_OTP_MATCH,
        }
    ),
    DisputeStatus.CARRIER_INVESTIGATION: frozenset(
        {
            DisputeStatus.GEO_LOCATION_VERIFIED,
            DisputeStatus.RESOLVED_REFUND,
            DisputeStatus.REJECTED_OTP_MATCH,
        }
    ),
    DisputeStatus.GEO_LOCATION_VERIFIED: frozenset(
        {DisputeStatus.RESOLVED_REFUND, DisputeStatus.REJECTED_OTP_MATCH}
    ),
    DisputeStatus.RESOLVED_REFUND: frozenset(),
    DisputeStatus.REJECTED_OTP_MATCH: frozenset(),
}


def advance[S: StrEnum](current: S, to: S, transitions: Mapping[S, frozenset[S]]) -> S:
    """Computes the next status, enforcing valid graph transitions."""
    if to not in transitions.get(current, frozenset()):
        raise IllegalTransition(current, to)
    return to


# ---------------------------------------------------------------------- Exports
__all__ = [
    # Typed IDs
    "CustomerId",
    "OrderId",
    "OrderItemId",
    "VariantId",
    "ReturnId",
    "RefundId",
    "DeliveryId",
    "DisputeId",
    # Value Objects
    "Money",
    "PhoneNumber",
    "Address",
    # Enums
    "OrderStatus",
    "OrderItemStatus",
    "PaymentMethod",
    "PaymentStatus",
    "PaymentCollectionMode",
    "ReturnPolicyType",
    "PolicyType",
    "Resolution",
    "ReturnReason",
    "ReturnRequestStatus",
    "ReturnStatus",
    "RefundMethod",
    "RefundStatus",
    "CustomerAccountStatus",
    "VerifiedChannel",
    "ProductCategory",
    "DeliveryStatus",
    "DeliverySlotPreference",
    "DisputeType",
    "DisputeStatus",
    "FeedbackTargetType",
    "FeedbackTag",
    # Entities & Payouts
    "RefundDestination",
    "Customer",
    "CustomerAccount",
    "Product",
    "ProductVariant",
    "OrderItemPolicySnapshot",
    "OrderItem",
    "Order",
    "Delivery",
    "ReturnRequest",
    "Refund",
    "DisputeTicket",
    "OrderFeedback",
    "DomainEvent",
    # Projections & Results
    "OrderSummary",
    "ItemDetail",
    "RefundView",
    "RefundInfo",
    "CancelResult",
    "ReturnResult",
    # Domain Logic & State Machines
    "DAMAGE_CLASS_REASONS",
    "IllegalTransition",
    "ORDER_TRANSITIONS",
    "RETURN_TRANSITIONS",
    "REFUND_TRANSITIONS",
    "DELIVERY_TRANSITIONS",
    "DISPUTE_TRANSITIONS",
    "advance",
]