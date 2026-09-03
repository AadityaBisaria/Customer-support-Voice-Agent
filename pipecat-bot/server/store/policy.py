"""Business policy: pure functions mirroring retail rules and SLAs.

These run with no database — the conversation layer calls them to decide
what to offer dynamically, and the store re-checks them inside mutation
transactions (defense in depth).
"""

from collections.abc import Sequence
from datetime import datetime, timedelta

from .domain import (
    DAMAGE_CLASS_REASONS,
    Delivery,
    DeliveryStatus,
    Order,
    OrderStatus,
    PaymentMethod,
    ProductCategory,
    RefundMethod,
    Resolution,
    ReturnPolicyType,
    ReturnReason,
    ReturnRequest,
    ReturnRequestStatus,
)


class PolicyError(Exception):
    """A rule said no. `code` is stable for tests; `message` is speakable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# Policy return windows
_NON_RETURNABLE_DAMAGE_WINDOW = timedelta(days=5)

_WINDOWS: dict[ReturnPolicyType, timedelta] = {
    ReturnPolicyType.RETURNABLE_10D: timedelta(days=10),
    ReturnPolicyType.RETURNABLE_30D: timedelta(days=30),
    ReturnPolicyType.REPLACEMENT_ONLY_7D: timedelta(days=7),
    ReturnPolicyType.REPLACEMENT_ONLY_10D: timedelta(days=10),
    ReturnPolicyType.STANDARD: timedelta(days=10),
    ReturnPolicyType.REPLACEMENT_ONLY: timedelta(days=7),
}


def return_window(policy: ReturnPolicyType) -> timedelta | None:
    """The policy's action window from delivery. None = non-returnable."""
    return _WINDOWS.get(policy)


def add_working_days(start: datetime, days: int) -> datetime:
    """Skip Saturdays and Sundays for SLA projections."""
    current = start
    remaining = days
    while remaining > 0:
        current = current + timedelta(days=1)
        if current.weekday() < 5:  # Monday to Friday
            remaining -= 1
    return current


# ----------------------------------------------------------- Order Cancellation
def check_cancellable(order: Order) -> None:
    """Cancellation is only allowed while the order has not shipped."""
    if order.status is OrderStatus.CANCELLED:
        raise PolicyError("already_cancelled", "This order is already cancelled.")
    if order.status is not OrderStatus.PLACED:
        raise PolicyError(
            "already_shipped",
            "This order has already shipped, so it cannot be cancelled. "
            "Once delivered, you can request a return or replacement instead.",
        )


# ---------------------------------------------------- Returns, Replacements & Exchanges
def valid_resolutions(
    *,
    policy: ReturnPolicyType,
    category: ProductCategory,
    reason: ReturnReason,
    delivered_at: datetime | None,
    now: datetime,
    same_variant_in_stock: bool,
    has_exchangeable_variants: bool,
    prior_requests: Sequence[ReturnRequest],
) -> frozenset[Resolution]:
    """Computes entitled resolutions or raises a speakable PolicyError."""
    if delivered_at is None:
        raise PolicyError(
            "not_delivered", "This item has not been delivered yet, so it cannot be returned."
        )

    # Active requests check
    active = [
        r
        for r in prior_requests
        if r.status not in (ReturnRequestStatus.COMPLETED, ReturnRequestStatus.REJECTED)
    ]
    if active:
        raise PolicyError(
            "already_active",
            f"There is already an open request for this item, reference {active[0].return_id}.",
        )

    damage_class = reason in DAMAGE_CLASS_REASONS
    window = return_window(policy)

    if window is None or policy is ReturnPolicyType.NON_RETURNABLE:
        if not damage_class:
            raise PolicyError(
                "non_returnable",
                "This item is non-returnable, and change-of-mind requests are not accepted for it.",
            )
        if now - delivered_at > _NON_RETURNABLE_DAMAGE_WINDOW:
            raise PolicyError(
                "window_closed",
                "For non-returnable items, issues must be reported within 5 days of delivery. That window has closed.",
            )
        offered = {Resolution.REFUND, Resolution.REPLACEMENT}
    else:
        if now - delivered_at > window:
            closed_on = (delivered_at + window).date().isoformat()
            raise PolicyError(
                "window_closed",
                f"The return window for this item closed on {closed_on}.",
            )
        if damage_class:
            offered = {Resolution.REFUND, Resolution.REPLACEMENT}
        elif policy in (ReturnPolicyType.RETURNABLE_10D, ReturnPolicyType.RETURNABLE_30D, ReturnPolicyType.STANDARD):
            offered = {Resolution.REFUND}
            # Allow exchange for apparel if other sizes/colors are in stock
            if category is ProductCategory.APPAREL and has_exchangeable_variants:
                offered.add(Resolution.EXCHANGE)
        else:
            raise PolicyError(
                "no_change_of_mind",
                "This item has a replacement-only policy and cannot be returned for a change of mind.",
            )

    # Prior replacement limit (no second replacements)
    replaced_before = any(
        r.resolution_type is Resolution.REPLACEMENT and r.status is not ReturnRequestStatus.REJECTED
        for r in prior_requests
    )
    if replaced_before:
        offered.discard(Resolution.REPLACEMENT)

    # Stock constraints
    if not same_variant_in_stock:
        offered.discard(Resolution.REPLACEMENT)

    if not offered:
        if replaced_before:
            raise PolicyError(
                "already_replaced",
                "This item was already replaced once, and a second replacement is not permitted.",
            )
        raise PolicyError(
            "out_of_stock",
            "The exact replacement item is out of stock, so a replacement cannot be offered.",
        )

    return frozenset(offered)


def explain_missing_replacement(
    *, same_variant_in_stock: bool, prior_requests: Sequence[ReturnRequest]
) -> PolicyError:
    """Explains why replacement is unavailable when the user explicitly requests it."""
    replaced_before = any(
        r.resolution_type is Resolution.REPLACEMENT and r.status is not ReturnRequestStatus.REJECTED
        for r in prior_requests
    )
    if replaced_before:
        return PolicyError(
            "already_replaced",
            "This item was already replaced once, and a second replacement cannot be issued.",
        )
    if not same_variant_in_stock:
        return PolicyError(
            "out_of_stock",
            "The item is currently out of stock with the seller, so a replacement is unavailable.",
        )
    return PolicyError("resolution_not_offered", "Replacement is not available for this product.")


# ------------------------------------------------------------ Logistics Reschedule
def check_reschedule_allowed(delivery: Delivery, new_date: datetime, now: datetime) -> None:
    """Enforces boundaries for customer-initiated delivery rescheduling."""
    if delivery.status in (DeliveryStatus.DELIVERED, DeliveryStatus.RETURNED_TO_ORIGIN):
        raise PolicyError("already_completed", "This delivery is already completed and cannot be rescheduled.")
    if delivery.status is DeliveryStatus.OUT_FOR_DELIVERY:
        raise PolicyError(
            "out_for_delivery",
            "The driver is already out for delivery today. If you are unavailable, the driver will re-attempt tomorrow.",
        )
    if delivery.reschedule_count >= 2:
        raise PolicyError(
            "max_reschedules_exceeded",
            "This shipment has reached the limit of two reschedules.",
        )
    if new_date.date() <= now.date():
        raise PolicyError("invalid_date", "The rescheduled delivery date must be at least one day in the future.")
    if (new_date - now).days > 7:
        raise PolicyError("date_too_far", "Deliveries cannot be postponed for more than 7 days.")


# ------------------------------------------------------------- Dispute Eligibility
def check_dispute_eligible(order: Order, delivery: Delivery | None, now: datetime) -> None:
    """Verifies eligibility for an Item Not Received (INR) dispute."""
    if order.status is not OrderStatus.DELIVERED:
        raise PolicyError("not_delivered", "A missing delivery dispute can only be filed after an order is marked delivered.")
    
    delivered_time = order.delivered_at or (delivery.actual_delivery_date if delivery else None)
    if delivered_time and (now - delivered_time).days > 3:
        raise PolicyError(
            "dispute_window_closed",
            "Disputes for missing deliveries must be reported within 3 days of the delivery notification.",
        )


# -------------------------------------------------------- Refund Timelines & Routes
def refund_expectation(
    payment_method: PaymentMethod,
    initiated_at: datetime,
    destination: RefundMethod | None = None,
) -> tuple[RefundMethod, datetime]:
    """Calculates refund channel and SLA date based on the payment method."""
    if payment_method in (PaymentMethod.CARD, PaymentMethod.UPI, PaymentMethod.NETBANKING):
        return RefundMethod.ORIGINAL_SOURCE, add_working_days(initiated_at, 5)

    if payment_method is PaymentMethod.CASH_ON_DELIVERY:
        if destination is RefundMethod.CHEQUE:
            return RefundMethod.CHEQUE, add_working_days(initiated_at, 10)
        if destination is RefundMethod.STORE_CREDIT:
            return RefundMethod.STORE_CREDIT, initiated_at + timedelta(hours=2)
        return RefundMethod.NEFT, add_working_days(initiated_at, 5)

    return RefundMethod.ORIGINAL_SOURCE, add_working_days(initiated_at, 5)