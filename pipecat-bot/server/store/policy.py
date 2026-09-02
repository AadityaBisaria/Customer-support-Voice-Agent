"""Business policy: pure functions mirroring the knowledge-base facts.

Each rule cites the corpus entry it mirrors (rag/corpus/qa.json). These run
with no database — the conversation layer calls them to decide what to offer,
and the store re-checks them inside the mutation transaction (defense in
depth: the DB never records something policy forbids).
"""

from collections.abc import Sequence
from datetime import datetime, timedelta

from .domain import (
    DAMAGE_CLASS_REASONS,
    Order,
    OrderStatus,
    PaymentMethod,
    PolicyType,
    RefundMethod,
    Resolution,
    ReturnReason,
    ReturnRequest,
    ReturnStatus,
)


class PolicyError(Exception):
    """A rule said no. `code` is stable for tests; `message` is speakable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# qa-018: even non-returnable items get help for damage-class issues, but you
# must report within 5 days of delivery.
_NON_RETURNABLE_DAMAGE_WINDOW = timedelta(days=5)

_WINDOWS: dict[PolicyType, timedelta] = {
    PolicyType.RETURNABLE_10D: timedelta(days=10),
    PolicyType.RETURNABLE_30D: timedelta(days=30),
    PolicyType.REPLACEMENT_ONLY_7D: timedelta(days=7),
    PolicyType.REPLACEMENT_ONLY_10D: timedelta(days=10),
}


def return_window(policy: PolicyType) -> timedelta | None:
    """The policy's action window from delivery. None = no general window
    (non-returnable; only the qa-018 damage window applies)."""
    return _WINDOWS.get(policy)


def check_cancellable(order: Order) -> None:
    """Cancellation is only possible before the order ships."""
    if order.status is OrderStatus.CANCELLED:
        raise PolicyError("already_cancelled", "This order is already cancelled.")
    if order.status is not OrderStatus.PLACED:
        raise PolicyError(
            "already_shipped",
            "This order has already shipped, so it can't be cancelled. "
            "Once it's delivered, a return may be possible instead.",
        )


def valid_resolutions(
    *,
    policy: PolicyType,
    reason: ReturnReason,
    delivered_at: datetime | None,
    now: datetime,
    same_variant_in_stock: bool,
    prior_requests: Sequence[ReturnRequest],
) -> frozenset[Resolution]:
    """Which resolutions this item is entitled to, or raise the most specific
    PolicyError when the answer is none.

    Rules mirrored: qa-022 (returnable windows incl. change of mind),
    qa-023/026 (replacement-only, no change-of-mind refund), qa-018/002/030
    (non-returnable but damage still covered, 5-day report window),
    qa-046 (no second replacement), qa-020/045 (replacement needs the exact
    item in stock, else refund), qa-032 (buyer's remorse per policy type).
    """
    if delivered_at is None:
        raise PolicyError(
            "not_delivered", "This item hasn't been delivered yet, so it can't be returned."
        )

    active = [
        r for r in prior_requests if r.status not in (ReturnStatus.COMPLETED, ReturnStatus.REJECTED)
    ]
    if active:
        raise PolicyError(
            "already_active",
            f"There's already an open request for this item, reference {active[0].id}.",
        )

    damage_class = reason in DAMAGE_CLASS_REASONS
    window = return_window(policy)

    if window is None:  # non-returnable
        if not damage_class:
            raise PolicyError(
                "non_returnable",
                "This item is non-returnable, and change-of-mind returns aren't covered for it.",
            )
        if now - delivered_at > _NON_RETURNABLE_DAMAGE_WINDOW:
            raise PolicyError(
                "window_closed",
                "For non-returnable items, damage has to be reported within 5 days of delivery, "
                "and that window has closed.",
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
        elif policy in (PolicyType.RETURNABLE_10D, PolicyType.RETURNABLE_30D):
            offered = {Resolution.REFUND}
        else:  # replacement-only policy, change-of-mind reason
            raise PolicyError(
                "no_change_of_mind",
                "This item has a replacement-only policy, so it can't be returned "
                "just for a change of mind.",
            )

    # qa-046: an item that was already replaced once can't be replaced again.
    replaced_before = any(
        r.resolution is Resolution.REPLACEMENT and r.status is not ReturnStatus.REJECTED
        for r in prior_requests
    )
    if replaced_before:
        offered.discard(Resolution.REPLACEMENT)
    # qa-020/045: replacement requires the exact same item in stock.
    if not same_variant_in_stock:
        offered.discard(Resolution.REPLACEMENT)

    if not offered:
        # Only reachable when damage-class replacement was the sole option and
        # it got discarded — offer nothing, say why.
        if replaced_before:
            raise PolicyError(
                "already_replaced",
                "This item was already replaced once, and a second replacement isn't possible.",
            )
        raise PolicyError(
            "out_of_stock",
            "The exact same item isn't in stock with the seller, so a replacement isn't possible.",
        )
    return frozenset(offered)


def explain_missing_replacement(
    *, same_variant_in_stock: bool, prior_requests: Sequence[ReturnRequest]
) -> PolicyError:
    """Why replacement specifically isn't on the table (qa-046, qa-020/045).

    Used when `valid_resolutions` offered refund but the caller wanted a
    replacement — both the conversation layer and the store speak with the
    same words.
    """
    replaced_before = any(
        r.resolution is Resolution.REPLACEMENT and r.status is not ReturnStatus.REJECTED
        for r in prior_requests
    )
    if replaced_before:
        return PolicyError(
            "already_replaced",
            "This item was already replaced once, and a second replacement isn't possible.",
        )
    if not same_variant_in_stock:
        return PolicyError(
            "out_of_stock",
            "The exact same item isn't in stock with the seller, so a replacement isn't possible.",
        )
    return PolicyError("resolution_not_offered", "That option isn't available for this item.")


def add_working_days(start: datetime, days: int) -> datetime:
    """Skip Saturdays and Sundays. Public holidays are documented out of scope."""
    current = start
    remaining = days
    while remaining > 0:
        current = current + timedelta(days=1)
        if current.weekday() < 5:  # Mon..Fri
            remaining -= 1
    return current


def refund_expectation(
    payment_method: PaymentMethod,
    initiated_at: datetime,
    destination: RefundMethod | None = None,
) -> tuple[RefundMethod, datetime]:
    """Refund route + expected-by, per the KB timelines (qa-034/035/036)."""
    if payment_method is PaymentMethod.AMAZON_PAY:
        return RefundMethod.AMAZON_PAY, initiated_at + timedelta(hours=4)
    if payment_method is PaymentMethod.CARD:
        return RefundMethod.CARD, add_working_days(initiated_at, 5)
    if payment_method is PaymentMethod.UPI:
        return RefundMethod.UPI, add_working_days(initiated_at, 5)
    if payment_method is PaymentMethod.NETBANKING:
        return RefundMethod.NETBANKING, add_working_days(initiated_at, 5)
    # Pay on Delivery: the caller chooses where the money goes (qa-036).
    if destination is RefundMethod.NEFT:
        return RefundMethod.NEFT, add_working_days(initiated_at, 5)
    if destination is RefundMethod.CHEQUE:
        return RefundMethod.CHEQUE, add_working_days(initiated_at, 10)
    raise PolicyError(
        "destination_required",
        "For Pay on Delivery orders the refund needs a destination: bank transfer or cheque.",
    )
