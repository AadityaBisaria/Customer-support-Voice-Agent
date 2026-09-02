"""SQLite implementation of SupportStore.

Sync core guarded by one lock + async facade via asyncio.to_thread — at one
session per process and sub-millisecond queries, an async driver buys nothing.
State tables are a projection; the append-only domain_events table is the
record of what happened, and its UNIQUE idempotency key makes every mutation
replay-safe: the same key returns the stored result verbatim.

Migrations run off PRAGMA user_version: connect() applies the missing tail of
MIGRATIONS inside a transaction and bumps the version.
"""

import asyncio
import json
import sqlite3
import threading
from collections.abc import Collection
from datetime import timedelta

from .clock import Clock, from_utc_iso, to_utc_iso
from .domain import (
    CancelResult,
    Customer,
    CustomerId,
    DomainEvent,
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
    Refund,
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
)
from .policy import (
    PolicyError,
    check_cancellable,
    explain_missing_replacement,
    refund_expectation,
    valid_resolutions,
)

PICKUP_LEAD = timedelta(days=2)

MIGRATIONS: list[str] = [
    # -- v1: initial schema -------------------------------------------------
    """
    CREATE TABLE customers (
      id INTEGER PRIMARY KEY, name TEXT NOT NULL,
      phone TEXT NOT NULL UNIQUE CHECK (length(phone) = 10),
      email TEXT
    );
    CREATE TABLE addresses (
      id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id),
      label TEXT NOT NULL, line1 TEXT NOT NULL, city TEXT NOT NULL,
      pincode TEXT NOT NULL CHECK (length(pincode) = 6)
    );
    CREATE TABLE products (
      id INTEGER PRIMARY KEY, title TEXT NOT NULL,
      price_paise INTEGER NOT NULL CHECK (price_paise >= 0),
      return_policy_type TEXT NOT NULL CHECK (return_policy_type IN
        ('returnable_10d','returnable_30d','replacement_only_7d','replacement_only_10d','non_returnable'))
    );
    CREATE TABLE product_variants (
      id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id),
      size TEXT, color TEXT, in_stock INTEGER NOT NULL DEFAULT 1 CHECK (in_stock IN (0,1))
    );
    CREATE TABLE orders (
      id TEXT PRIMARY KEY,
      customer_id INTEGER NOT NULL REFERENCES customers(id),
      address_id  INTEGER NOT NULL REFERENCES addresses(id),
      status TEXT NOT NULL CHECK (status IN ('placed','shipped','out_for_delivery','delivered','cancelled')),
      payment_method TEXT NOT NULL CHECK (payment_method IN ('amazon_pay','card','upi','netbanking','pod')),
      placed_at TEXT NOT NULL, shipped_at TEXT, delivered_at TEXT,
      total_paise INTEGER NOT NULL CHECK (total_paise >= 0),
      CHECK (status NOT IN ('shipped','out_for_delivery','delivered') OR shipped_at IS NOT NULL),
      CHECK (status <> 'delivered' OR delivered_at IS NOT NULL)
    );
    CREATE TABLE order_items (
      id INTEGER PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(id),
      variant_id INTEGER NOT NULL REFERENCES product_variants(id),
      quantity INTEGER NOT NULL DEFAULT 1 CHECK (quantity > 0),
      price_paise INTEGER NOT NULL CHECK (price_paise >= 0)
    );
    CREATE TABLE return_requests (
      id TEXT PRIMARY KEY,
      order_item_id INTEGER NOT NULL REFERENCES order_items(id),
      resolution TEXT NOT NULL CHECK (resolution IN ('refund','replacement')),
      reason TEXT NOT NULL CHECK (reason IN
        ('damaged','defective','wrong_item','missing_parts','not_needed','size_issue')),
      status TEXT NOT NULL DEFAULT 'requested' CHECK (status IN
        ('requested','pickup_scheduled','picked_up','completed','rejected')),
      replacement_variant_id INTEGER REFERENCES product_variants(id),
      created_at TEXT NOT NULL,
      CHECK (resolution <> 'replacement' OR replacement_variant_id IS NOT NULL)
    );
    CREATE UNIQUE INDEX one_active_return_per_item ON return_requests(order_item_id)
      WHERE status NOT IN ('completed','rejected');
    CREATE TABLE refunds (
      id TEXT PRIMARY KEY,
      order_id TEXT NOT NULL REFERENCES orders(id),
      return_request_id TEXT REFERENCES return_requests(id),
      origin TEXT NOT NULL CHECK (origin IN ('return','cancellation')),
      amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
      method TEXT NOT NULL CHECK (method IN ('amazon_pay','card','upi','netbanking','neft','cheque')),
      status TEXT NOT NULL DEFAULT 'initiated' CHECK (status IN ('initiated','processed','completed')),
      initiated_at TEXT NOT NULL, expected_by TEXT NOT NULL,
      CHECK ((origin = 'return') = (return_request_id IS NOT NULL))
    );
    CREATE TABLE domain_events (
      seq             INTEGER PRIMARY KEY AUTOINCREMENT,
      occurred_at     TEXT NOT NULL,
      aggregate_type  TEXT NOT NULL,
      aggregate_id    TEXT NOT NULL,
      event_type      TEXT NOT NULL,
      payload_json    TEXT NOT NULL,
      idempotency_key TEXT UNIQUE
    );
    CREATE INDEX idx_orders_customer  ON orders(customer_id, status);
    CREATE INDEX idx_items_order      ON order_items(order_id);
    CREATE INDEX idx_returns_item     ON return_requests(order_item_id);
    CREATE INDEX idx_refunds_order    ON refunds(order_id);
    CREATE INDEX idx_events_aggregate ON domain_events(aggregate_type, aggregate_id);
    """,
]


def connect(path: str = ":memory:") -> sqlite3.Connection:
    """Open a connection with migrations applied (idempotent)."""
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, script in enumerate(MIGRATIONS[version:], start=version):
        # No explicit transaction: executescript() commits any open one itself.
        conn.executescript(script)
        conn.execute(f"PRAGMA user_version = {i + 1}")
    return conn


# --------------------------------------------------- result serialization
# Mutation results round-trip through the event log so replays are verbatim.


def _refund_info_to_json(info: RefundInfo | None) -> dict | None:
    if info is None:
        return None
    return {
        "refund_id": info.refund_id,
        "amount_paise": info.amount.paise,
        "method": info.method.value,
        "expected_by": to_utc_iso(info.expected_by),
    }


def _refund_info_from_json(data: dict | None) -> RefundInfo | None:
    if data is None:
        return None
    return RefundInfo(
        refund_id=RefundId(data["refund_id"]),
        amount=Money(data["amount_paise"]),
        method=RefundMethod(data["method"]),
        expected_by=from_utc_iso(data["expected_by"]),
    )


def cancel_result_to_json(result: CancelResult) -> str:
    return json.dumps({"order_id": result.order_id, "refund": _refund_info_to_json(result.refund)})


def cancel_result_from_json(raw: str) -> CancelResult:
    data = json.loads(raw)
    return CancelResult(
        order_id=OrderId(data["order_id"]), refund=_refund_info_from_json(data["refund"])
    )


def return_result_to_json(result: ReturnResult) -> str:
    return json.dumps(
        {
            "return_id": result.return_id,
            "order_item_id": result.order_item_id,
            "resolution": result.resolution.value,
            "refund": _refund_info_to_json(result.refund),
            "pickup_by": to_utc_iso(result.pickup_by),
        }
    )


def return_result_from_json(raw: str) -> ReturnResult:
    data = json.loads(raw)
    return ReturnResult(
        return_id=ReturnId(data["return_id"]),
        order_item_id=OrderItemId(data["order_item_id"]),
        resolution=Resolution(data["resolution"]),
        refund=_refund_info_from_json(data["refund"]),
        pickup_by=from_utc_iso(data["pickup_by"]),
    )


# ------------------------------------------------------------ row mapping


def _order_from_row(row: sqlite3.Row, items: tuple[OrderItem, ...] = ()) -> Order:
    return Order(
        id=OrderId(row["id"]),
        customer_id=CustomerId(row["customer_id"]),
        address_id=row["address_id"],
        status=OrderStatus(row["status"]),
        payment_method=PaymentMethod(row["payment_method"]),
        placed_at=from_utc_iso(row["placed_at"]),
        shipped_at=from_utc_iso(row["shipped_at"]) if row["shipped_at"] else None,
        delivered_at=from_utc_iso(row["delivered_at"]) if row["delivered_at"] else None,
        total=Money(row["total_paise"]),
        items=items,
    )


def _item_from_row(row: sqlite3.Row) -> OrderItem:
    return OrderItem(
        id=OrderItemId(row["id"]),
        order_id=OrderId(row["order_id"]),
        variant_id=VariantId(row["variant_id"]),
        quantity=row["quantity"],
        price=Money(row["price_paise"]),
    )


def _return_from_row(row: sqlite3.Row) -> ReturnRequest:
    return ReturnRequest(
        id=ReturnId(row["id"]),
        order_item_id=OrderItemId(row["order_item_id"]),
        resolution=Resolution(row["resolution"]),
        reason=ReturnReason(row["reason"]),
        status=ReturnStatus(row["status"]),
        replacement_variant_id=(
            VariantId(row["replacement_variant_id"]) if row["replacement_variant_id"] else None
        ),
        created_at=from_utc_iso(row["created_at"]),
    )


def _refund_from_row(row: sqlite3.Row) -> Refund:
    return Refund(
        id=RefundId(row["id"]),
        order_id=OrderId(row["order_id"]),
        return_request_id=ReturnId(row["return_request_id"]) if row["return_request_id"] else None,
        origin=row["origin"],
        amount=Money(row["amount_paise"]),
        method=RefundMethod(row["method"]),
        status=RefundStatus(row["status"]),
        initiated_at=from_utc_iso(row["initiated_at"]),
        expected_by=from_utc_iso(row["expected_by"]),
    )


class SqliteSupportStore:
    """SupportStore over SQLite. Async facade; sync core under one lock."""

    def __init__(self, *, clock: Clock, path: str = ":memory:") -> None:
        self._clock = clock
        self._conn = connect(path)
        self._lock = threading.Lock()

    @classmethod
    def seeded_in_memory(cls, clock: Clock) -> "SqliteSupportStore":
        from .seed import seed

        store = cls(clock=clock, path=":memory:")
        with store._lock:
            seed(store._conn, clock.now())
        return store

    # ----------------------------------------------------------- reads

    async def customer_by_phone(self, phone: PhoneNumber) -> Customer | None:
        return await asyncio.to_thread(self._customer_by_phone, phone)

    def _customer_by_phone(self, phone: PhoneNumber) -> Customer | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM customers WHERE phone = ?", (phone.digits,)
            ).fetchone()
        if row is None:
            return None
        return Customer(
            id=CustomerId(row["id"]),
            name=row["name"],
            phone=PhoneNumber(row["phone"]),
            email=row["email"],
        )

    async def orders_for_customer(
        self, customer_id: CustomerId, *, statuses: Collection[OrderStatus] | None = None
    ) -> list[OrderSummary]:
        return await asyncio.to_thread(self._orders_for_customer, customer_id, statuses)

    def _orders_for_customer(
        self, customer_id: CustomerId, statuses: Collection[OrderStatus] | None
    ) -> list[OrderSummary]:
        query = "SELECT * FROM orders WHERE customer_id = ?"
        args: list = [customer_id]
        if statuses:
            marks = ",".join("?" for _ in statuses)
            query += f" AND status IN ({marks})"
            args.extend(s.value for s in statuses)
        query += " ORDER BY placed_at DESC"
        with self._lock:
            rows = self._conn.execute(query, args).fetchall()
            summaries = []
            for row in rows:
                titles = self._conn.execute(
                    "SELECT p.title FROM order_items oi "
                    "JOIN product_variants v ON v.id = oi.variant_id "
                    "JOIN products p ON p.id = v.product_id WHERE oi.order_id = ? ORDER BY oi.id",
                    (row["id"],),
                ).fetchall()
                summaries.append(
                    OrderSummary(
                        id=OrderId(row["id"]),
                        status=OrderStatus(row["status"]),
                        payment_method=PaymentMethod(row["payment_method"]),
                        placed_at=from_utc_iso(row["placed_at"]),
                        delivered_at=from_utc_iso(row["delivered_at"])
                        if row["delivered_at"]
                        else None,
                        item_titles=tuple(t["title"] for t in titles),
                        total=Money(row["total_paise"]),
                    )
                )
        return summaries

    async def order_with_items(self, order_id: OrderId) -> Order:
        return await asyncio.to_thread(self._order_with_items, order_id)

    def _order_with_items(self, order_id: OrderId) -> Order:
        with self._lock:
            row = self._conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if row is None:
                raise KeyError(f"no such order: {order_id}")
            item_rows = self._conn.execute(
                "SELECT * FROM order_items WHERE order_id = ? ORDER BY id", (order_id,)
            ).fetchall()
        return _order_from_row(row, tuple(_item_from_row(r) for r in item_rows))

    async def items_for_order(self, order_id: OrderId) -> list[ItemDetail]:
        return await asyncio.to_thread(self._items_for_order, order_id)

    def _items_for_order(self, order_id: OrderId) -> list[ItemDetail]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT oi.id AS oi_id, oi.order_id, oi.variant_id, oi.quantity, oi.price_paise,"
                "       v.id AS v_id, v.product_id, v.size, v.color, v.in_stock,"
                "       p.id AS p_id, p.title, p.price_paise AS p_price, p.return_policy_type "
                "FROM order_items oi "
                "JOIN product_variants v ON v.id = oi.variant_id "
                "JOIN products p ON p.id = v.product_id "
                "WHERE oi.order_id = ? ORDER BY oi.id",
                (order_id,),
            ).fetchall()
        details = []
        for r in rows:
            details.append(
                ItemDetail(
                    item=OrderItem(
                        id=OrderItemId(r["oi_id"]),
                        order_id=OrderId(r["order_id"]),
                        variant_id=VariantId(r["variant_id"]),
                        quantity=r["quantity"],
                        price=Money(r["price_paise"]),
                    ),
                    variant=ProductVariant(
                        id=VariantId(r["v_id"]),
                        product_id=r["product_id"],
                        size=r["size"],
                        color=r["color"],
                        in_stock=bool(r["in_stock"]),
                    ),
                    product=Product(
                        id=r["p_id"],
                        title=r["title"],
                        price=Money(r["p_price"]),
                        return_policy_type=PolicyType(r["return_policy_type"]),
                    ),
                )
            )
        return details

    async def return_requests_for_item(self, order_item_id: OrderItemId) -> list[ReturnRequest]:
        return await asyncio.to_thread(self._return_requests_for_item, order_item_id)

    def _return_requests_for_item(self, order_item_id: OrderItemId) -> list[ReturnRequest]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM return_requests WHERE order_item_id = ? ORDER BY created_at",
                (order_item_id,),
            ).fetchall()
        return [_return_from_row(r) for r in rows]

    async def refunds_for_customer(self, customer_id: CustomerId) -> list[RefundView]:
        return await asyncio.to_thread(self._refunds_for_customer, customer_id)

    def _refunds_for_customer(self, customer_id: CustomerId) -> list[RefundView]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT r.* FROM refunds r JOIN orders o ON o.id = r.order_id "
                "WHERE o.customer_id = ? ORDER BY r.initiated_at DESC",
                (customer_id,),
            ).fetchall()
            views = []
            for row in rows:
                titles = self._conn.execute(
                    "SELECT p.title FROM order_items oi "
                    "JOIN product_variants v ON v.id = oi.variant_id "
                    "JOIN products p ON p.id = v.product_id WHERE oi.order_id = ?",
                    (row["order_id"],),
                ).fetchall()
                views.append(
                    RefundView(
                        refund=_refund_from_row(row),
                        order_item_titles=tuple(t["title"] for t in titles),
                    )
                )
        return views

    async def variant_in_stock(self, variant_id: VariantId) -> bool:
        return await asyncio.to_thread(self._variant_in_stock, variant_id)

    def _variant_in_stock(self, variant_id: VariantId) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT in_stock FROM product_variants WHERE id = ?", (variant_id,)
            ).fetchone()
        return bool(row and row["in_stock"])

    async def events_for_aggregate(
        self, aggregate_type: str, aggregate_id: str
    ) -> list[DomainEvent]:
        return await asyncio.to_thread(self._events_for_aggregate, aggregate_type, aggregate_id)

    def _events_for_aggregate(self, aggregate_type: str, aggregate_id: str) -> list[DomainEvent]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM domain_events WHERE aggregate_type = ? AND aggregate_id = ? ORDER BY seq",
                (aggregate_type, aggregate_id),
            ).fetchall()
        return [
            DomainEvent(
                seq=r["seq"],
                occurred_at=from_utc_iso(r["occurred_at"]),
                aggregate_type=r["aggregate_type"],
                aggregate_id=r["aggregate_id"],
                event_type=r["event_type"],
                payload=json.loads(r["payload_json"]),
                idempotency_key=r["idempotency_key"],
            )
            for r in rows
        ]

    # ------------------------------------------------------- mutations
    # One BEGIN IMMEDIATE transaction each: replay-check -> compare-and-swap
    # precondition -> state rows -> event append -> commit.

    async def cancel_order(self, *, order_id: OrderId, idempotency_key: str) -> CancelResult:
        return await asyncio.to_thread(self._cancel_order, order_id, idempotency_key)

    def _cancel_order(self, order_id: OrderId, idempotency_key: str) -> CancelResult:
        now = self._clock.now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                replay = self._replay(idempotency_key)
                if replay is not None:
                    self._conn.execute("COMMIT")
                    return cancel_result_from_json(replay)

                row = self._conn.execute(
                    "SELECT * FROM orders WHERE id = ?", (order_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(f"no such order: {order_id}")
                order = _order_from_row(row)
                check_cancellable(order)

                # Compare-and-swap: a race that shipped the order in the
                # meantime makes rowcount 0 — nothing is written.
                cur = self._conn.execute(
                    "UPDATE orders SET status = 'cancelled' WHERE id = ? AND status = 'placed'",
                    (order_id,),
                )
                if cur.rowcount != 1:
                    raise PolicyError(
                        "state_changed", "The order's status just changed; nothing was done."
                    )

                refund_info: RefundInfo | None = None
                if order.payment_method is not PaymentMethod.POD:
                    method, expected_by = refund_expectation(order.payment_method, now)
                    refund_id = self._next_refund_id()
                    self._conn.execute(
                        "INSERT INTO refunds (id, order_id, return_request_id, origin, amount_paise,"
                        " method, status, initiated_at, expected_by) VALUES (?,?,NULL,'cancellation',?,?,"
                        " 'initiated', ?, ?)",
                        (
                            refund_id,
                            order_id,
                            order.total.paise,
                            method.value,
                            to_utc_iso(now),
                            to_utc_iso(expected_by),
                        ),
                    )
                    refund_info = RefundInfo(
                        refund_id=RefundId(refund_id),
                        amount=order.total,
                        method=method,
                        expected_by=expected_by,
                    )

                result = CancelResult(order_id=order_id, refund=refund_info)
                self._append_event(
                    now,
                    "order",
                    order_id,
                    "order_cancelled",
                    cancel_result_to_json(result),
                    idempotency_key,
                )
                self._conn.execute("COMMIT")
                return result
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    async def create_return(
        self,
        *,
        order_item_id: OrderItemId,
        reason: ReturnReason,
        refund_destination: RefundMethod | None,
        idempotency_key: str,
    ) -> ReturnResult:
        return await asyncio.to_thread(
            self._create_case,
            order_item_id,
            reason,
            Resolution.REFUND,
            refund_destination,
            idempotency_key,
        )

    async def create_replacement(
        self, *, order_item_id: OrderItemId, reason: ReturnReason, idempotency_key: str
    ) -> ReturnResult:
        return await asyncio.to_thread(
            self._create_case, order_item_id, reason, Resolution.REPLACEMENT, None, idempotency_key
        )

    def _create_case(
        self,
        order_item_id: OrderItemId,
        reason: ReturnReason,
        resolution: Resolution,
        refund_destination: RefundMethod | None,
        idempotency_key: str,
    ) -> ReturnResult:
        now = self._clock.now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                replay = self._replay(idempotency_key)
                if replay is not None:
                    self._conn.execute("COMMIT")
                    return return_result_from_json(replay)

                r = self._conn.execute(
                    "SELECT oi.*, o.payment_method, o.delivered_at, o.id AS o_id,"
                    "       v.in_stock, p.return_policy_type "
                    "FROM order_items oi JOIN orders o ON o.id = oi.order_id "
                    "JOIN product_variants v ON v.id = oi.variant_id "
                    "JOIN products p ON p.id = v.product_id WHERE oi.id = ?",
                    (order_item_id,),
                ).fetchone()
                if r is None:
                    raise KeyError(f"no such order item: {order_item_id}")

                prior = self._return_requests_for_item_locked(order_item_id)
                # Defense in depth: the confirm gate already validated this,
                # but the DB never records what policy forbids.
                offered = valid_resolutions(
                    policy=PolicyType(r["return_policy_type"]),
                    reason=reason,
                    delivered_at=from_utc_iso(r["delivered_at"]) if r["delivered_at"] else None,
                    now=now,
                    same_variant_in_stock=bool(r["in_stock"]),
                    prior_requests=prior,
                )
                if resolution not in offered:
                    if resolution is Resolution.REPLACEMENT:
                        raise explain_missing_replacement(
                            same_variant_in_stock=bool(r["in_stock"]), prior_requests=prior
                        )
                    raise PolicyError(
                        "resolution_not_offered", "That option isn't available for this item."
                    )

                return_id = self._next_return_id()
                self._conn.execute(
                    "INSERT INTO return_requests (id, order_item_id, resolution, reason, status,"
                    " replacement_variant_id, created_at) VALUES (?,?,?,?, 'requested', ?, ?)",
                    (
                        return_id,
                        order_item_id,
                        resolution.value,
                        reason.value,
                        r["variant_id"] if resolution is Resolution.REPLACEMENT else None,
                        to_utc_iso(now),
                    ),
                )

                refund_info: RefundInfo | None = None
                if resolution is Resolution.REFUND:
                    method, expected_by = refund_expectation(
                        PaymentMethod(r["payment_method"]), now, refund_destination
                    )
                    amount = Money(r["price_paise"]) * r["quantity"]
                    refund_id = self._next_refund_id()
                    self._conn.execute(
                        "INSERT INTO refunds (id, order_id, return_request_id, origin, amount_paise,"
                        " method, status, initiated_at, expected_by) VALUES (?,?,?,'return',?,?,"
                        " 'initiated', ?, ?)",
                        (
                            refund_id,
                            r["o_id"],
                            return_id,
                            amount.paise,
                            method.value,
                            to_utc_iso(now),
                            to_utc_iso(expected_by),
                        ),
                    )
                    refund_info = RefundInfo(
                        refund_id=RefundId(refund_id),
                        amount=amount,
                        method=method,
                        expected_by=expected_by,
                    )

                result = ReturnResult(
                    return_id=ReturnId(return_id),
                    order_item_id=order_item_id,
                    resolution=resolution,
                    refund=refund_info,
                    pickup_by=now + PICKUP_LEAD,
                )
                self._append_event(
                    now,
                    "return_case",
                    return_id,
                    "return_requested",
                    return_result_to_json(result),
                    idempotency_key,
                )
                self._conn.execute("COMMIT")
                return result
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    # ------------------------------------------------------- internals

    def _return_requests_for_item_locked(self, order_item_id: OrderItemId) -> list[ReturnRequest]:
        rows = self._conn.execute(
            "SELECT * FROM return_requests WHERE order_item_id = ? ORDER BY created_at",
            (order_item_id,),
        ).fetchall()
        return [_return_from_row(r) for r in rows]

    def _replay(self, idempotency_key: str) -> str | None:
        row = self._conn.execute(
            "SELECT payload_json FROM domain_events WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        return row["payload_json"] if row else None

    def _append_event(
        self,
        now,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        payload_json: str,
        idempotency_key: str | None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO domain_events (occurred_at, aggregate_type, aggregate_id, event_type,"
            " payload_json, idempotency_key) VALUES (?,?,?,?,?,?)",
            (
                to_utc_iso(now),
                aggregate_type,
                aggregate_id,
                event_type,
                payload_json,
                idempotency_key,
            ),
        )

    def _next_return_id(self) -> str:
        count = self._conn.execute("SELECT COUNT(*) FROM return_requests").fetchone()[0]
        return f"RET-{1001 + count}"

    def _next_refund_id(self) -> str:
        count = self._conn.execute("SELECT COUNT(*) FROM refunds").fetchone()[0]
        return f"REF-{2001 + count}"
