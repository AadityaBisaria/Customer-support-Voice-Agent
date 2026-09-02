"""Policy rules, table-driven against the KB facts they mirror."""

from datetime import datetime, timedelta

import pytest

from store.clock import IST
from store.domain import (
    Money,
    Order,
    OrderId,
    OrderItemId,
    OrderStatus,
    PaymentMethod,
    PolicyType,
    RefundMethod,
    Resolution,
    ReturnId,
    ReturnReason,
    ReturnRequest,
    ReturnStatus,
    VariantId,
)
from store.policy import (
    PolicyError,
    check_cancellable,
    refund_expectation,
    return_window,
    valid_resolutions,
)

# A Wednesday noon, IST — makes working-day math easy to reason about.
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=IST)


def order(status: OrderStatus, payment: PaymentMethod = PaymentMethod.CARD) -> Order:
    shipped = NOW - timedelta(days=2) if status is not OrderStatus.PLACED else None
    delivered = NOW - timedelta(days=1) if status is OrderStatus.DELIVERED else None
    return Order(
        id=OrderId("AMZ-X"),
        customer_id=1,
        address_id=1,
        status=status,  # type: ignore[arg-type]
        payment_method=payment,
        placed_at=NOW - timedelta(days=3),
        shipped_at=shipped,
        delivered_at=delivered,
        total=Money.rupees(100),
    )


def prior(resolution: Resolution, status: ReturnStatus) -> ReturnRequest:
    return ReturnRequest(
        id=ReturnId("RET-P"),
        order_item_id=OrderItemId(1),
        resolution=resolution,
        reason=ReturnReason.DEFECTIVE,
        status=status,
        replacement_variant_id=VariantId(1) if resolution is Resolution.REPLACEMENT else None,
        created_at=NOW - timedelta(days=1),
    )


class TestCancellable:
    def test_placed_is_cancellable(self):
        check_cancellable(order(OrderStatus.PLACED))

    @pytest.mark.parametrize(
        "status, code",
        [
            (OrderStatus.SHIPPED, "already_shipped"),
            (OrderStatus.OUT_FOR_DELIVERY, "already_shipped"),
            (OrderStatus.DELIVERED, "already_shipped"),
            (OrderStatus.CANCELLED, "already_cancelled"),
        ],
    )
    def test_not_cancellable(self, status, code):
        with pytest.raises(PolicyError) as e:
            check_cancellable(order(status))
        assert e.value.code == code


class TestValidResolutions:
    def run(self, *, policy, reason, delivered_days_ago, in_stock=True, priors=()):
        return valid_resolutions(
            policy=policy,
            reason=reason,
            delivered_at=NOW - timedelta(days=delivered_days_ago),
            now=NOW,
            same_variant_in_stock=in_stock,
            prior_requests=list(priors),
        )

    # qa-022: returnable windows, change of mind allowed
    def test_returnable_change_of_mind_gets_refund_only(self):
        assert self.run(
            policy=PolicyType.RETURNABLE_30D, reason=ReturnReason.NOT_NEEDED, delivered_days_ago=5
        ) == frozenset({Resolution.REFUND})

    # damage-class unlocks both resolutions
    def test_returnable_damage_gets_both(self):
        assert self.run(
            policy=PolicyType.RETURNABLE_10D, reason=ReturnReason.DAMAGED, delivered_days_ago=3
        ) == frozenset({Resolution.REFUND, Resolution.REPLACEMENT})

    # qa-023: replacement-only rejects change of mind
    def test_replacement_only_rejects_change_of_mind(self):
        with pytest.raises(PolicyError) as e:
            self.run(
                policy=PolicyType.REPLACEMENT_ONLY_7D,
                reason=ReturnReason.SIZE_ISSUE,
                delivered_days_ago=2,
            )
        assert e.value.code == "no_change_of_mind"

    # qa-018: damage on replacement-only still gets both
    def test_replacement_only_damage_gets_both(self):
        assert self.run(
            policy=PolicyType.REPLACEMENT_ONLY_10D,
            reason=ReturnReason.DEFECTIVE,
            delivered_days_ago=4,
        ) == frozenset({Resolution.REFUND, Resolution.REPLACEMENT})

    # windows close
    @pytest.mark.parametrize(
        "policy, days",
        [
            (PolicyType.RETURNABLE_10D, 11),
            (PolicyType.RETURNABLE_30D, 31),
            (PolicyType.REPLACEMENT_ONLY_7D, 8),
        ],
    )
    def test_window_closed(self, policy, days):
        with pytest.raises(PolicyError) as e:
            self.run(policy=policy, reason=ReturnReason.DAMAGED, delivered_days_ago=days)
        assert e.value.code == "window_closed"

    # qa-002/030: non-returnable rejects change of mind outright
    def test_non_returnable_change_of_mind(self):
        with pytest.raises(PolicyError) as e:
            self.run(
                policy=PolicyType.NON_RETURNABLE,
                reason=ReturnReason.NOT_NEEDED,
                delivered_days_ago=1,
            )
        assert e.value.code == "non_returnable"

    # qa-018: non-returnable damage covered, within 5 days
    def test_non_returnable_damage_within_5_days(self):
        assert self.run(
            policy=PolicyType.NON_RETURNABLE, reason=ReturnReason.DAMAGED, delivered_days_ago=4
        ) == frozenset({Resolution.REFUND, Resolution.REPLACEMENT})

    def test_non_returnable_damage_after_5_days(self):
        with pytest.raises(PolicyError) as e:
            self.run(
                policy=PolicyType.NON_RETURNABLE, reason=ReturnReason.DAMAGED, delivered_days_ago=6
            )
        assert e.value.code == "window_closed"

    # qa-046: no second replacement
    def test_already_replaced_drops_replacement(self):
        assert self.run(
            policy=PolicyType.RETURNABLE_10D,
            reason=ReturnReason.DAMAGED,
            delivered_days_ago=2,
            priors=[prior(Resolution.REPLACEMENT, ReturnStatus.COMPLETED)],
        ) == frozenset({Resolution.REFUND})

    def test_replacement_only_already_replaced_is_refund_only(self):
        # Damage on a replacement-only item that was already replaced: refund survives.
        assert self.run(
            policy=PolicyType.REPLACEMENT_ONLY_7D,
            reason=ReturnReason.DEFECTIVE,
            delivered_days_ago=2,
            priors=[prior(Resolution.REPLACEMENT, ReturnStatus.COMPLETED)],
        ) == frozenset({Resolution.REFUND})

    # qa-020/045: out of stock drops replacement
    def test_out_of_stock_drops_replacement(self):
        assert self.run(
            policy=PolicyType.RETURNABLE_10D,
            reason=ReturnReason.DAMAGED,
            delivered_days_ago=2,
            in_stock=False,
        ) == frozenset({Resolution.REFUND})

    def test_active_request_blocks_new_one(self):
        with pytest.raises(PolicyError) as e:
            self.run(
                policy=PolicyType.RETURNABLE_30D,
                reason=ReturnReason.DAMAGED,
                delivered_days_ago=2,
                priors=[prior(Resolution.REFUND, ReturnStatus.REQUESTED)],
            )
        assert e.value.code == "already_active"

    def test_undelivered_rejected(self):
        with pytest.raises(PolicyError) as e:
            valid_resolutions(
                policy=PolicyType.RETURNABLE_10D,
                reason=ReturnReason.DAMAGED,
                delivered_at=None,
                now=NOW,
                same_variant_in_stock=True,
                prior_requests=[],
            )
        assert e.value.code == "not_delivered"


class TestRefundExpectation:
    def test_amazon_pay_is_four_hours(self):  # qa-034
        method, expected = refund_expectation(PaymentMethod.AMAZON_PAY, NOW)
        assert method is RefundMethod.AMAZON_PAY
        assert expected == NOW + timedelta(hours=4)

    @pytest.mark.parametrize(
        "pm, rm",
        [
            (PaymentMethod.CARD, RefundMethod.CARD),
            (PaymentMethod.UPI, RefundMethod.UPI),
            (PaymentMethod.NETBANKING, RefundMethod.NETBANKING),
        ],
    )
    def test_prepaid_is_five_working_days(self, pm, rm):  # qa-035
        method, expected = refund_expectation(pm, NOW)
        assert method is rm
        # Wednesday + 5 working days skips one weekend -> next Wednesday.
        assert expected == NOW + timedelta(days=7)

    def test_working_days_span_weekend(self):
        friday = datetime(2026, 9, 4, 12, 0, tzinfo=IST)
        _, expected = refund_expectation(PaymentMethod.CARD, friday)
        assert expected == friday + timedelta(days=7)  # Fri -> next Fri

    def test_pod_needs_destination(self):  # qa-036
        with pytest.raises(PolicyError) as e:
            refund_expectation(PaymentMethod.POD, NOW)
        assert e.value.code == "destination_required"

    def test_pod_neft_and_cheque(self):  # qa-036
        method, expected = refund_expectation(PaymentMethod.POD, NOW, RefundMethod.NEFT)
        assert method is RefundMethod.NEFT and expected == NOW + timedelta(days=7)
        method, expected = refund_expectation(PaymentMethod.POD, NOW, RefundMethod.CHEQUE)
        assert method is RefundMethod.CHEQUE and expected == NOW + timedelta(days=14)


def test_return_window_values():
    assert return_window(PolicyType.RETURNABLE_10D) == timedelta(days=10)
    assert return_window(PolicyType.RETURNABLE_30D) == timedelta(days=30)
    assert return_window(PolicyType.NON_RETURNABLE) is None
