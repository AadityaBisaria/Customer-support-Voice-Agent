"""SQLite store: migrations, seed integrity, mutations, replay safety."""

import sqlite3
from datetime import datetime, timedelta

import pytest

from store.clock import IST, FixedClock, to_utc_iso
from store.domain import (
    Money,
    OrderId,
    OrderItemId,
    OrderStatus,
    PhoneNumber,
    RefundMethod,
    Resolution,
    ReturnReason,
)
from store.policy import PolicyError
from store.sqlite import MIGRATIONS, SqliteSupportStore, connect

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=IST)  # Wednesday


@pytest.fixture()
def store() -> SqliteSupportStore:
    return SqliteSupportStore.seeded_in_memory(FixedClock(NOW))


class TestMigrations:
    def test_connect_twice_is_idempotent(self, tmp_path):
        path = str(tmp_path / "store.db")
        conn1 = connect(path)
        version1 = conn1.execute("PRAGMA user_version").fetchone()[0]
        conn1.close()
        conn2 = connect(path)  # re-applying must be a no-op
        version2 = conn2.execute("PRAGMA user_version").fetchone()[0]
        conn2.close()
        assert version1 == version2 == len(MIGRATIONS)

    def test_check_constraints_bite(self):
        conn = connect()
        with pytest.raises(sqlite3.IntegrityError):
            # delivered status without delivered_at violates the coherence CHECK
            conn.execute("INSERT INTO customers (id, name, phone) VALUES (1, 'X', '1234567890')")
            conn.execute(
                "INSERT INTO addresses (id, customer_id, label, line1, city, pincode)"
                " VALUES (1, 1, 'home', 'x', 'y', '123456')"
            )
            conn.execute(
                "INSERT INTO orders (id, customer_id, address_id, status, payment_method,"
                " placed_at, total_paise) VALUES ('O1', 1, 1, 'delivered', 'card', 'now', 100)"
            )


class TestSeedIntegrity:
    async def test_customers_by_phone(self, store):
        priya = await store.customer_by_phone(PhoneNumber("9876543210"))
        assert priya is not None and priya.name == "Priya Sharma"
        assert await store.customer_by_phone(PhoneNumber("9999999999")) is None

    async def test_priya_has_four_orders_and_vikram_none(self, store):
        priya = await store.customer_by_phone(PhoneNumber("9876543210"))
        vikram = await store.customer_by_phone(PhoneNumber("9111111111"))
        assert len(await store.orders_for_customer(priya.id)) == 4
        assert await store.orders_for_customer(vikram.id) == []

    async def test_status_filter(self, store):
        rahul = await store.customer_by_phone(PhoneNumber("9123456789"))
        placed = await store.orders_for_customer(rahul.id, statuses={OrderStatus.PLACED})
        assert [o.id for o in placed] == ["AMZ-2003"]

    async def test_two_item_order(self, store):
        items = await store.items_for_order(OrderId("AMZ-1001"))
        assert len(items) == 2
        assert items[0].product.title == "Anarkali kurta set"

    async def test_out_of_stock_variant(self, store):
        items = await store.items_for_order(OrderId("AMZ-3003"))
        assert not items[0].variant.in_stock

    async def test_seeded_refund_visible(self, store):
        anita = await store.customer_by_phone(PhoneNumber("9000000001"))
        views = await store.refunds_for_customer(anita.id)
        assert [v.refund.id for v in views] == ["REF-2001"]
        assert views[0].order_item_titles == ("laptop backpack",)


class TestCancelOrder:
    async def test_prepaid_cancel_creates_refund(self, store):
        result = await store.cancel_order(order_id=OrderId("AMZ-1004"), idempotency_key="k1")
        assert result.refund is not None
        assert result.refund.method is RefundMethod.AMAZON_PAY  # qa-034
        assert result.refund.expected_by == NOW + timedelta(hours=4)
        assert result.refund.amount == Money(39900)
        order = await store.order_with_items(OrderId("AMZ-1004"))
        assert order.status is OrderStatus.CANCELLED

    async def test_pod_cancel_has_no_refund_row(self, store):
        result = await store.cancel_order(order_id=OrderId("AMZ-2003"), idempotency_key="k2")
        assert result.refund is None
        rahul = await store.customer_by_phone(PhoneNumber("9123456789"))
        assert all(
            v.refund.order_id != "AMZ-2003" for v in await store.refunds_for_customer(rahul.id)
        )

    async def test_idempotent_replay(self, store):
        first = await store.cancel_order(order_id=OrderId("AMZ-1004"), idempotency_key="same")
        second = await store.cancel_order(order_id=OrderId("AMZ-1004"), idempotency_key="same")
        assert first == second  # byte-identical payload round-trip
        priya = await store.customer_by_phone(PhoneNumber("9876543210"))
        refunds = [
            v for v in await store.refunds_for_customer(priya.id) if v.refund.order_id == "AMZ-1004"
        ]
        assert len(refunds) == 1  # exactly one refund row despite the replay

    async def test_shipped_order_refuses(self, store):
        with pytest.raises(PolicyError) as e:
            await store.cancel_order(order_id=OrderId("AMZ-2002"), idempotency_key="k3")
        assert e.value.code == "already_shipped"
        # And no event was recorded for the refused mutation.
        assert await store.events_for_aggregate("order", "AMZ-2002") == []


class TestReturns:
    async def test_create_return_with_refund(self, store):
        # AMZ-1001 item 1: kurta, returnable_30d, delivered -5d, card.
        result = await store.create_return(
            order_item_id=OrderItemId(1),
            reason=ReturnReason.NOT_NEEDED,
            refund_destination=None,
            idempotency_key="r1",
        )
        assert result.resolution is Resolution.REFUND
        assert result.refund is not None and result.refund.method is RefundMethod.CARD
        assert result.refund.amount == Money(189900)
        assert result.pickup_by == NOW + timedelta(days=2)

    async def test_pod_return_defaults_to_upi(self, store):
        result = await store.create_return(
            order_item_id=OrderItemId(6),
            reason=ReturnReason.NOT_NEEDED,
            refund_destination=None,
            idempotency_key="r3",
        )
        assert result.refund.method is RefundMethod.UPI
        assert result.refund.expected_by == NOW + timedelta(days=7)

    async def test_replacement_happy_path(self, store):
        result = await store.create_replacement(
            order_item_id=OrderItemId(4),
            reason=ReturnReason.DAMAGED,
            idempotency_key="r4",
        )
        assert result.resolution is Resolution.REPLACEMENT
        assert result.refund is None

    async def test_out_of_window_refused(self, store):
        with pytest.raises(PolicyError) as e:
            await store.create_return(
                order_item_id=OrderItemId(9),
                reason=ReturnReason.NOT_NEEDED,
                refund_destination=None,
                idempotency_key="r5",
            )
        assert e.value.code == "window_closed"

    async def test_second_replacement_refused(self, store):
        with pytest.raises(PolicyError) as e:
            await store.create_replacement(
                order_item_id=OrderItemId(10),
                reason=ReturnReason.DEFECTIVE,
                idempotency_key="r6",
            )
        assert e.value.code == "already_replaced"

    async def test_out_of_stock_replacement_refused_but_refund_works(self, store):
        with pytest.raises(PolicyError) as e:
            await store.create_replacement(
                order_item_id=OrderItemId(11),
                reason=ReturnReason.DAMAGED,
                idempotency_key="r7",
            )
        assert e.value.code == "out_of_stock"
        result = await store.create_return(
            order_item_id=OrderItemId(11),
            reason=ReturnReason.DAMAGED,
            refund_destination=None,
            idempotency_key="r8",
        )
        assert result.resolution is Resolution.REFUND

    async def test_active_request_blocks_second(self, store):
        await store.create_return(
            order_item_id=OrderItemId(1),
            reason=ReturnReason.NOT_NEEDED,
            refund_destination=None,
            idempotency_key="r9",
        )
        with pytest.raises(PolicyError) as e:
            await store.create_return(
                order_item_id=OrderItemId(1),
                reason=ReturnReason.DAMAGED,
                refund_destination=None,
                idempotency_key="r10",
            )
        assert e.value.code == "already_active"

    async def test_partial_index_is_the_backstop(self, store):
        """Even bypassing policy, the DB rejects a second ACTIVE case per item."""
        await store.create_return(
            order_item_id=OrderItemId(1),
            reason=ReturnReason.NOT_NEEDED,
            refund_destination=None,
            idempotency_key="r11",
        )
        with pytest.raises(sqlite3.IntegrityError):
            store._conn.execute(
                "INSERT INTO return_requests (id, order_item_id, resolution, reason, status,"
                " replacement_variant_id, created_at) VALUES"
                " ('RET-X', 1, 'refund', 'damaged', 'requested', NULL, ?)",
                (to_utc_iso(NOW),),
            )

    async def test_return_replay_is_verbatim(self, store):
        first = await store.create_return(
            order_item_id=OrderItemId(3),
            reason=ReturnReason.SIZE_ISSUE,
            refund_destination=None,
            idempotency_key="dup",
        )
        second = await store.create_return(
            order_item_id=OrderItemId(3),
            reason=ReturnReason.SIZE_ISSUE,
            refund_destination=None,
            idempotency_key="dup",
        )
        assert first == second

    async def test_event_log_records_the_mutation(self, store):
        result = await store.create_replacement(
            order_item_id=OrderItemId(4),
            reason=ReturnReason.DAMAGED,
            idempotency_key="ev1",
        )
        events = await store.events_for_aggregate("return_case", result.return_id)
        assert [e.event_type for e in events] == ["return_requested"]
        assert events[0].idempotency_key == "ev1"
