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
from datetime import datetime, timedelta

from schemas.models import (
    CancelResult as SchemaCancelResult,
    RefundInfo as SchemaRefundInfo,
    ReturnResult as SchemaReturnResult,
)
from .clock import Clock, from_utc_iso, to_utc_iso
from .domain import (
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
    DisputeTicket,
    DomainEvent,
    ItemDetail,
    Money,
    Order,
    OrderFeedback,
    OrderId,
    OrderItem,
    OrderItemId,
    OrderStatus,
    OrderSummary,
    PaymentCollectionMode,
    PaymentMethod,
    PhoneNumber,
    PolicyType,
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
    ReturnReason,
    ReturnRequest,
    ReturnResult,
    ReturnStatus,
    VariantId,
    VerifiedChannel,
)
from .policy import (
    PolicyError,
    check_cancellable,
    check_reschedule_allowed,
    explain_missing_replacement,
    refund_expectation,
    valid_resolutions,
)

PICKUP_LEAD = timedelta(days=2)
_SQLITE_BUSY_TIMEOUT_MS = 2_000
_SQLITE_WRITE_RETRY_DELAYS = (0.05, 0.20)

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
      category TEXT NOT NULL DEFAULT 'other',
      price_paise INTEGER NOT NULL CHECK (price_paise >= 0),
      return_policy_type TEXT NOT NULL CHECK (return_policy_type IN
        ('returnable_10d','returnable_30d','replacement_only_7d','replacement_only_10d','standard','replacement_only','non_returnable'))
    );
    CREATE TABLE product_variants (
      id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id),
      size TEXT, color TEXT, in_stock INTEGER NOT NULL DEFAULT 1 CHECK (in_stock IN (0,1))
    );
    CREATE TABLE orders (
      id TEXT PRIMARY KEY,
      customer_id INTEGER NOT NULL REFERENCES customers(id),
      address_id  INTEGER NOT NULL REFERENCES addresses(id),
      status TEXT NOT NULL CHECK (status IN ('placed','confirmed','shipped','out_for_delivery','delivered','cancelled','returned')),
      payment_method TEXT NOT NULL CHECK (payment_method IN ('card','upi','netbanking','cash_on_delivery','pod')),
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
      resolution TEXT NOT NULL CHECK (resolution IN ('refund','replacement','exchange')),
      reason TEXT NOT NULL CHECK (reason IN
        ('damaged','defective','wrong_item','missing_parts','not_needed','size_issue')),
      status TEXT NOT NULL DEFAULT 'requested' CHECK (status IN
        ('requested','initiated','pickup_scheduled','picked_up','inspected','completed','rejected')),
      replacement_variant_id INTEGER REFERENCES product_variants(id),
      created_at TEXT NOT NULL,
      CHECK (resolution NOT IN ('replacement','exchange') OR replacement_variant_id IS NOT NULL)
    );
    CREATE UNIQUE INDEX one_active_return_per_item ON return_requests(order_item_id)
      WHERE status NOT IN ('completed','rejected');
    CREATE TABLE refunds (
      id TEXT PRIMARY KEY,
      order_id TEXT NOT NULL REFERENCES orders(id),
      return_request_id TEXT REFERENCES return_requests(id),
      origin TEXT NOT NULL CHECK (origin IN ('return','cancellation')),
      amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
      method TEXT NOT NULL CHECK (method IN ('original_source','card','upi','netbanking','neft','cheque','store_credit')),
      status TEXT NOT NULL DEFAULT 'initiated' CHECK (status IN ('initiated','processing','processed','completed','failed')),
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
    # -- v2: deliveries, customer accounts, disputes, and feedback ----------
    """
    CREATE TABLE customer_accounts (
        account_id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL UNIQUE REFERENCES customers(id),
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','flagged','suspended')),
        verified_channels_json TEXT NOT NULL DEFAULT '[]',
        auth_failure_count INTEGER NOT NULL DEFAULT 0,
        default_refund_destination_json TEXT
    );
    CREATE TABLE deliveries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id TEXT NOT NULL UNIQUE REFERENCES orders(id),
        courier_partner TEXT,
        tracking_number TEXT,
        status TEXT NOT NULL CHECK (status IN ('manifested','in_transit','out_for_delivery','delivered','failed_attempt','returned_to_origin')),
        estimated_delivery_date TEXT,
        actual_delivery_date TEXT,
        rescheduled_delivery_date TEXT,
        reschedule_count INTEGER NOT NULL DEFAULT 0,
        delivery_slot_preference TEXT CHECK (delivery_slot_preference IN ('morning','afternoon','evening')),
        delivery_instructions TEXT,
        latest_checkpoint_text TEXT,
        failed_attempt_reason TEXT
    );
    CREATE TABLE dispute_tickets (
        id TEXT PRIMARY KEY,
        order_id TEXT NOT NULL REFERENCES orders(id),
        delivery_id INTEGER REFERENCES deliveries(id),
        customer_id INTEGER NOT NULL REFERENCES customers(id),
        dispute_type TEXT NOT NULL CHECK (dispute_type IN ('item_not_received','empty_box','wrong_location')),
        status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','carrier_investigation','geo_location_verified','resolved_refund','rejected_otp_match')),
        reported_at TEXT NOT NULL,
        sla_resolution_deadline TEXT NOT NULL,
        investigation_notes_json TEXT NOT NULL DEFAULT '[]'
    );
    CREATE TABLE order_feedback (
        id TEXT PRIMARY KEY,
        order_id TEXT NOT NULL REFERENCES orders(id),
        customer_id INTEGER NOT NULL REFERENCES customers(id),
        target_type TEXT NOT NULL CHECK (target_type IN ('overall_order','delivery_experience','specific_item')),
        target_item_id INTEGER REFERENCES order_items(id),
        rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
        category_tags_json TEXT NOT NULL DEFAULT '[]',
        spoken_comment TEXT,
        created_at TEXT NOT NULL
    );
    ALTER TABLE orders ADD COLUMN payment_collection_mode TEXT CHECK (payment_collection_mode IN ('cash','static_qr','pre_delivery_payment_link'));
    ALTER TABLE orders ADD COLUMN pre_delivery_payment_link TEXT;
    CREATE INDEX idx_deliveries_order ON deliveries(order_id);
    CREATE INDEX idx_disputes_customer ON dispute_tickets(customer_id);
    """,
]


def connect(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(
        path,
        check_same_thread=False,
        isolation_level=None,
        timeout=_SQLITE_BUSY_TIMEOUT_MS / 1000,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {_SQLITE_BUSY_TIMEOUT_MS}")
    # WAL lets SQLite readers (including DB Browser for SQLite) see a stable
    # snapshot while the bot commits a mutation. A second writer still
    # serializes, which is why write calls also have a bounded retry below.
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, script in enumerate(MIGRATIONS[version:], start=version):
        conn.executescript(script)
        conn.execute(f"PRAGMA user_version = {i + 1}")
    return conn


def cancel_result_to_json(result: CancelResult) -> str:
    payload = SchemaCancelResult.model_validate(result, from_attributes=True)
    return payload.model_dump_json()


def cancel_result_from_json(raw: str) -> CancelResult:
    data = SchemaCancelResult.model_validate_json(raw)
    refund = None
    if data.refund is not None:
        refund = RefundInfo(
            refund_id=RefundId(data.refund.refund_id),
            amount=Money(paise=data.refund.amount.paise),
            method=RefundMethod(data.refund.method),
            expected_by=data.refund.expected_by,
        )
    return CancelResult(order_id=OrderId(data.order_id), refund=refund)


def return_result_to_json(result: ReturnResult) -> str:
    payload = SchemaReturnResult.model_validate(result, from_attributes=True)
    return payload.model_dump_json()


def return_result_from_json(raw: str) -> ReturnResult:
    data = SchemaReturnResult.model_validate_json(raw)
    refund = None
    if data.refund is not None:
        refund = RefundInfo(
            refund_id=RefundId(data.refund.refund_id),
            amount=Money(data.refund.amount.paise),
            method=RefundMethod(data.refund.method),
            expected_by=data.refund.expected_by,
        )
    return ReturnResult(
        return_id=ReturnId(data.return_id),
        order_item_id=OrderItemId(data.order_item_id),
        resolution=Resolution(data.resolution),
        refund=refund,
        pickup_by=data.pickup_by,
    )


def _order_from_row(row: sqlite3.Row, items: tuple[OrderItem, ...] = ()) -> Order:
    return Order(
        order_id=OrderId(row["id"]),
        customer_id=CustomerId(row["customer_id"]),
        status=OrderStatus(row["status"]),
        items=items,
        subtotal=Money(paise=row["total_paise"]),
        tax_amount=Money(paise=0),
        shipping_fee=Money(paise=0),
        total_amount=Money(paise=row["total_paise"]),
        payment_method=PaymentMethod(row["payment_method"]),
        placed_at=from_utc_iso(row["placed_at"]),
        delivered_at=from_utc_iso(row["delivered_at"]) if row["delivered_at"] else None,
        payment_collection_mode=(
            PaymentCollectionMode(row["payment_collection_mode"])
            if ("payment_collection_mode" in row.keys() and row["payment_collection_mode"])
            else None
        ),
        pre_delivery_payment_link=(
            row["pre_delivery_payment_link"]
            if ("pre_delivery_payment_link" in row.keys() and row["pre_delivery_payment_link"])
            else None
        ),
    )


def _item_from_row(row: sqlite3.Row) -> OrderItem:
    return OrderItem(
        order_item_id=OrderItemId(row["id"]),
        order_id=OrderId(row["order_id"]),
        product_id=row["product_id"] if "product_id" in row.keys() else 0,
        variant_id=VariantId(row["variant_id"]),
        title=row["title"] if "title" in row.keys() else "",
        quantity=row["quantity"],
        unit_price=Money(paise=row["price_paise"]),
        total_price=Money(paise=row["price_paise"] * row["quantity"]),
    )


def _return_from_row(row: sqlite3.Row) -> ReturnRequest:
    return ReturnRequest(
        return_id=ReturnId(row["id"]),
        order_id=OrderId(row["order_id"]) if "order_id" in row.keys() else OrderId(""),
        order_item_id=OrderItemId(row["order_item_id"]),
        resolution_type=Resolution(row["resolution"]),
        reason=ReturnReason(row["reason"]),
        status=ReturnStatus(row["status"]),
        pickup_date=from_utc_iso(row["created_at"]),
    )


def _refund_from_row(row: sqlite3.Row) -> Refund:
    return Refund(
        refund_id=RefundId(row["id"]),
        order_id=OrderId(row["order_id"]),
        return_id=ReturnId(row["return_request_id"]) if row["return_request_id"] else None,
        amount=Money(paise=row["amount_paise"]),
        status=RefundStatus(row["status"]),
        method=RefundMethod(row["method"]),
        initiated_at=from_utc_iso(row["initiated_at"]),
        expected_by_date=from_utc_iso(row["expected_by"]),
    )


class SqliteSupportStore:
    def __init__(self, *, clock: Clock, path: str = ":memory:") -> None:
        self._clock = clock
        self._conn = connect(path)
        # Transaction helpers may call read helpers on the same connection/thread.
        self._lock = threading.RLock()

    @classmethod
    def seeded_in_memory(cls, clock: Clock) -> "SqliteSupportStore":
        from .seed import seed

        store = cls(clock=clock, path=":memory:")
        with store._lock:
            seed(store._conn, clock.now())
        return store

    @classmethod
    def seeded_at_path(cls, clock: Clock, path: str) -> "SqliteSupportStore":
        """Open a demo database and seed it only when it has no customers.

        Unlike :meth:`seeded_in_memory`, this preserves domain events and
        mutations made by earlier calls. The empty-database check makes first
        launch idempotent, so reconnecting never restores a cancelled order.
        """
        from .seed import seed

        store = cls(clock=clock, path=path)
        with store._lock:
            has_seed_data = store._conn.execute(
                "SELECT 1 FROM customers LIMIT 1"
            ).fetchone()
            if has_seed_data is None:
                seed(store._conn, clock.now())
        return store

    @staticmethod
    def _is_busy_error(error: sqlite3.OperationalError) -> bool:
        return "locked" in str(error).casefold() or "busy" in str(error).casefold()

    async def _write_with_retry(self, operation, *args):
        """Retry only transient SQLite lock contention; mutations stay idempotent."""
        for attempt, delay in enumerate((*_SQLITE_WRITE_RETRY_DELAYS, None)):
            try:
                return await asyncio.to_thread(operation, *args)
            except sqlite3.OperationalError as error:
                if not self._is_busy_error(error) or delay is None:
                    raise
                await asyncio.sleep(delay)

    # ----------------------------------------------------------- Reads
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
            customer_id=CustomerId(row["id"]),
            first_name=row["name"].split()[0],
            last_name=" ".join(row["name"].split()[1:]) if " " in row["name"] else "",
            primary_phone=row["phone"],
            email=row["email"],
        )

    async def account_for_customer(self, customer_id: CustomerId) -> CustomerAccount | None:
        return await asyncio.to_thread(self._account_for_customer, customer_id)

    def _account_for_customer(self, customer_id: CustomerId) -> CustomerAccount | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM customer_accounts WHERE customer_id = ?", (customer_id,)
            ).fetchone()
        if row is None:
            return None
        dest_raw = row["default_refund_destination_json"]
        dest = json.loads(dest_raw) if dest_raw else None
        return CustomerAccount(
            account_id=row["account_id"],
            customer_id=CustomerId(row["customer_id"]),
            status=CustomerAccountStatus(row["status"]),
            verified_channels=tuple(
                VerifiedChannel(c) for c in json.loads(row["verified_channels_json"])
            ),
            auth_failure_count=row["auth_failure_count"],
            default_refund_destination=dest,
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
                        order_id=OrderId(row["id"]),
                        status=OrderStatus(row["status"]),
                        payment_method=PaymentMethod(row["payment_method"]),
                        placed_at=from_utc_iso(row["placed_at"]),
                        delivered_at=(
                            from_utc_iso(row["delivered_at"]) if row["delivered_at"] else None
                        ),
                        item_titles=tuple(t["title"] for t in titles),
                        total_amount=Money(paise=row["total_paise"]),
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
                "SELECT oi.*, p.title, v.product_id FROM order_items oi "
                "JOIN product_variants v ON v.id = oi.variant_id "
                "JOIN products p ON p.id = v.product_id "
                "WHERE oi.order_id = ? ORDER BY oi.id",
                (order_id,),
            ).fetchall()
        return _order_from_row(row, tuple(_item_from_row(r) for r in item_rows))

    async def items_for_order(self, order_id: OrderId) -> list[ItemDetail]:
        return await asyncio.to_thread(self._items_for_order, order_id)

    def _items_for_order(self, order_id: OrderId) -> list[ItemDetail]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT oi.id AS oi_id, oi.order_id, oi.variant_id, oi.quantity, oi.price_paise,"
                "       v.id AS v_id, v.product_id, v.size, v.color, v.in_stock,"
                "       p.id AS p_id, p.title, p.category, p.price_paise AS p_price, p.return_policy_type "
                "FROM order_items oi "
                "JOIN product_variants v ON v.id = oi.variant_id "
                "JOIN products p ON p.id = v.product_id "
                "WHERE oi.order_id = ? ORDER BY oi.id",
                (order_id,),
            ).fetchall()
        return [
            ItemDetail(
                item=OrderItem(
                    order_item_id=OrderItemId(r["oi_id"]),
                    order_id=OrderId(r["order_id"]),
                    product_id=r["product_id"],
                    variant_id=VariantId(r["variant_id"]),
                    title=r["title"],
                    quantity=r["quantity"],
                    unit_price=Money(paise=r["price_paise"]),
                    total_price=Money(paise=r["price_paise"] * r["quantity"]),
                ),
                variant=ProductVariant(
                    variant_id=VariantId(r["v_id"]),
                    product_id=r["product_id"],
                    sku=f"SKU-{r['v_id']}",
                    attributes={"size": r["size"] or "", "color": r["color"] or ""},
                    stock_count=10 if r["in_stock"] else 0,
                    price=Money(paise=r["price_paise"]),
                ),
                product=Product(
                    product_id=r["p_id"],
                    title=r["title"],
                    category=ProductCategory(r["category"]) if "category" in r.keys() else ProductCategory.OTHER,
                    return_policy_type=PolicyType(r["return_policy_type"]),
                ),
            )
            for r in rows
        ]

    async def delivery_for_order(self, order_id: OrderId) -> Delivery | None:
        return await asyncio.to_thread(self._delivery_for_order, order_id)

    def _delivery_for_order(self, order_id: OrderId) -> Delivery | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM deliveries WHERE order_id = ?", (order_id,)
            ).fetchone()
        if row is None:
            return None
        return Delivery(
            delivery_id=DeliveryId(row["id"]),
            order_id=OrderId(row["order_id"]),
            courier_partner=row["courier_partner"],
            tracking_number=row["tracking_number"],
            status=DeliveryStatus(row["status"]),
            estimated_delivery_date=(
                from_utc_iso(row["estimated_delivery_date"])
                if row["estimated_delivery_date"]
                else None
            ),
            actual_delivery_date=(
                from_utc_iso(row["actual_delivery_date"]) if row["actual_delivery_date"] else None
            ),
            rescheduled_delivery_date=(
                from_utc_iso(row["rescheduled_delivery_date"])
                if row["rescheduled_delivery_date"]
                else None
            ),
            reschedule_count=row["reschedule_count"],
            delivery_slot_preference=(
                DeliverySlotPreference(row["delivery_slot_preference"])
                if row["delivery_slot_preference"]
                else None
            ),
            delivery_instructions=row["delivery_instructions"],
            latest_checkpoint_text=row["latest_checkpoint_text"],
            failed_attempt_reason=row["failed_attempt_reason"],
        )

    async def variants_for_product(self, product_id: int) -> list[ProductVariant]:
        return await asyncio.to_thread(self._variants_for_product, product_id)

    def _variants_for_product(self, product_id: int) -> list[ProductVariant]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM product_variants WHERE product_id = ?", (product_id,)
            ).fetchall()
        return [
            ProductVariant(
                variant_id=VariantId(r["id"]),
                product_id=r["product_id"],
                sku=f"SKU-{r['id']}",
                attributes={"size": r["size"] or "", "color": r["color"] or ""},
                stock_count=10 if r["in_stock"] else 0,
                price=Money(paise=0),
            )
            for r in rows
        ]

    async def return_requests_for_item(self, order_item_id: OrderItemId) -> list[ReturnRequest]:
        return await asyncio.to_thread(self._return_requests_for_item, order_item_id)

    def _return_requests_for_item(self, order_item_id: OrderItemId) -> list[ReturnRequest]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT rr.*, oi.order_id FROM return_requests rr "
                "JOIN order_items oi ON oi.id = rr.order_item_id "
                "WHERE rr.order_item_id = ? ORDER BY rr.created_at",
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

    # ------------------------------------------------------- Mutations
    async def cancel_order(self, *, order_id: OrderId, idempotency_key: str) -> CancelResult:
        return await self._write_with_retry(self._cancel_order, order_id, idempotency_key)

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

                cur = self._conn.execute(
                    "UPDATE orders SET status = 'cancelled' WHERE id = ? AND status = 'placed'",
                    (order_id,),
                )
                if cur.rowcount != 1:
                    raise PolicyError(
                        "state_changed", "The order's status just changed; nothing was done."
                    )

                refund_info: RefundInfo | None = None
                if order.payment_method not in (
                    PaymentMethod.CASH_ON_DELIVERY,
                ):
                    method, expected_by = refund_expectation(order.payment_method, now)
                    refund_id = self._next_refund_id()
                    self._conn.execute(
                        "INSERT INTO refunds (id, order_id, return_request_id, origin, amount_paise,"
                        " method, status, initiated_at, expected_by) VALUES (?,?,NULL,'cancellation',?,?,"
                        " 'initiated', ?, ?)",
                        (
                            refund_id,
                            order_id,
                            order.total_amount.paise,
                            method.value,
                            to_utc_iso(now),
                            to_utc_iso(expected_by),
                        ),
                    )
                    refund_info = RefundInfo(
                        refund_id=RefundId(refund_id),
                        amount=order.total_amount,
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
        refund_destination: RefundDestination | None,
        idempotency_key: str,
    ) -> ReturnResult:
        return await self._write_with_retry(
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
        return await self._write_with_retry(
            self._create_case, order_item_id, reason, Resolution.REPLACEMENT, None, idempotency_key
        )

    def _create_case(
        self,
        order_item_id: OrderItemId,
        reason: ReturnReason,
        resolution: Resolution,
        refund_destination: RefundDestination | None,
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
                    "       v.product_id, v.in_stock, p.category, p.return_policy_type "
                    "FROM order_items oi JOIN orders o ON o.id = oi.order_id "
                    "JOIN product_variants v ON v.id = oi.variant_id "
                    "JOIN products p ON p.id = v.product_id WHERE oi.id = ?",
                    (order_item_id,),
                ).fetchone()
                if r is None:
                    raise KeyError(f"no such order item: {order_item_id}")

                prior = self._return_requests_for_item_locked(order_item_id)
                category = (
                    ProductCategory(r["category"])
                    if "category" in r.keys() and r["category"]
                    else ProductCategory.OTHER
                )
                exchangeable = self._has_exchangeable_variants_locked(
                    r["product_id"], r["variant_id"]
                )

                offered = valid_resolutions(
                    policy=PolicyType(r["return_policy_type"]),
                    category=category,
                    reason=reason,
                    delivered_at=from_utc_iso(r["delivered_at"]) if r["delivered_at"] else None,
                    now=now,
                    same_variant_in_stock=bool(r["in_stock"]),
                    has_exchangeable_variants=exchangeable,
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
                    refund_method_enum = None
                    if refund_destination:
                        refund_method_enum = (
                            RefundMethod.UPI
                            if getattr(refund_destination, "kind", None) == "upi"
                            else RefundMethod.NEFT
                        )
                    method, expected_by = refund_expectation(
                        PaymentMethod(r["payment_method"]), now, refund_method_enum
                    )
                    amount = Money(paise=r["price_paise"]) * r["quantity"]
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

    async def create_exchange(
        self,
        *,
        order_item_id: OrderItemId,
        new_variant_id: VariantId,
        reason: ReturnReason,
        idempotency_key: str,
    ) -> ReturnResult:
        return await self._write_with_retry(
            self._create_exchange, order_item_id, new_variant_id, reason, idempotency_key
        )

    def _create_exchange(
        self,
        order_item_id: OrderItemId,
        new_variant_id: VariantId,
        reason: ReturnReason,
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

                target = self._conn.execute(
                    'SELECT * FROM product_variants WHERE id = ?', (new_variant_id,)
                ).fetchone()
                current = self._conn.execute(
                    'SELECT oi.variant_id, v.product_id, v.in_stock, p.category, '
                    'p.return_policy_type, o.delivered_at FROM order_items oi '
                    'JOIN product_variants v ON v.id = oi.variant_id '
                    'JOIN products p ON p.id = v.product_id '
                    'JOIN orders o ON o.id = oi.order_id WHERE oi.id = ?', (order_item_id,)
                ).fetchone()
                if not target or not target['in_stock']:
                    raise PolicyError(
                        "out_of_stock", "The requested size/color is currently out of stock."
                    )
                if (not current or target['product_id'] != current['product_id']
                        or new_variant_id == current['variant_id']):
                    raise PolicyError('invalid_variant', 'That variant does not match this item.')
                offered = valid_resolutions(
                    policy=PolicyType(current['return_policy_type']),
                    category=ProductCategory(current['category']), reason=reason,
                    delivered_at=(from_utc_iso(current['delivered_at'])
                                  if current['delivered_at'] else None), now=now,
                    same_variant_in_stock=bool(current['in_stock']),
                    has_exchangeable_variants=True,
                    prior_requests=self._return_requests_for_item_locked(order_item_id),
                )
                if Resolution.EXCHANGE not in offered:
                    raise PolicyError('resolution_not_offered', 'Exchange is unavailable for this item.')

                return_id = self._next_return_id()
                self._conn.execute(
                    "INSERT INTO return_requests (id, order_item_id, resolution, reason, status,"
                    " replacement_variant_id, created_at) VALUES (?,?,?,?, 'requested', ?, ?)",
                    (
                        return_id,
                        order_item_id,
                        Resolution.EXCHANGE.value,
                        reason.value,
                        new_variant_id,
                        to_utc_iso(now),
                    ),
                )
                result = ReturnResult(
                    return_id=ReturnId(return_id),
                    order_item_id=order_item_id,
                    resolution=Resolution.EXCHANGE,
                    refund=None,
                    pickup_by=now + PICKUP_LEAD,
                )
                self._append_event(
                    now,
                    "return_case",
                    return_id,
                    "exchange_requested",
                    return_result_to_json(result),
                    idempotency_key,
                )
                self._conn.execute("COMMIT")
                return result
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    async def reschedule_delivery(
        self,
        *,
        delivery_id: DeliveryId,
        new_date: datetime,
        slot: DeliverySlotPreference | None,
        instructions: str | None,
        idempotency_key: str,
    ) -> Delivery:
        return await self._write_with_retry(
            self._reschedule_delivery, delivery_id, new_date, slot, instructions, idempotency_key
        )

    def _reschedule_delivery(
        self,
        delivery_id: DeliveryId,
        new_date: datetime,
        slot: DeliverySlotPreference | None,
        instructions: str | None,
        idempotency_key: str,
    ) -> Delivery:
        now = self._clock.now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                replay = self._replay(idempotency_key)
                if replay is not None:
                    self._conn.execute('COMMIT')
                    return Delivery.model_validate_json(replay)
                row = self._conn.execute(
                    "SELECT * FROM deliveries WHERE id = ?", (delivery_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(f"no such delivery: {delivery_id}")

                current = Delivery(
                    delivery_id=DeliveryId(row["id"]),
                    order_id=OrderId(row["order_id"]),
                    status=DeliveryStatus(row["status"]),
                    reschedule_count=row["reschedule_count"],
                    estimated_delivery_date=(
                        from_utc_iso(row["estimated_delivery_date"])
                        if row["estimated_delivery_date"]
                        else None
                    ),
                )
                check_reschedule_allowed(current, new_date, now)

                self._conn.execute(
                    "UPDATE deliveries SET rescheduled_delivery_date = ?, reschedule_count = reschedule_count + 1, "
                    "delivery_slot_preference = ?, delivery_instructions = ? WHERE id = ?",
                    (to_utc_iso(new_date), slot.value if slot else None, instructions, delivery_id),
                )
                updated_row = self._conn.execute(
                    "SELECT * FROM deliveries WHERE id = ?", (delivery_id,)
                ).fetchone()
                updated_delivery = self._delivery_for_order(OrderId(updated_row["order_id"]))

                self._append_event(
                    now,
                    "delivery",
                    str(delivery_id),
                    "delivery_rescheduled",
                    updated_delivery.model_dump_json(),
                    idempotency_key,
                )
                self._conn.execute("COMMIT")
                return updated_delivery
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    async def update_payment_collection_mode(
        self,
        *,
        order_id: OrderId,
        mode: PaymentCollectionMode,
        idempotency_key: str,
    ) -> Order:
        return await self._write_with_retry(
            self._update_payment_collection_mode, order_id, mode, idempotency_key
        )

    def _update_payment_collection_mode(
        self,
        order_id: OrderId,
        mode: PaymentCollectionMode,
        idempotency_key: str,
    ) -> Order:
        now = self._clock.now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "UPDATE orders SET payment_collection_mode = ? WHERE id = ?",
                    (mode.value, order_id),
                )
                updated_order = self._order_with_items(order_id)
                self._append_event(
                    now,
                    "order",
                    order_id,
                    "payment_mode_updated",
                    json.dumps({"mode": mode.value}),
                    idempotency_key,
                )
                self._conn.execute("COMMIT")
                return updated_order
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    async def submit_dispute_ticket(
        self, *, ticket: DisputeTicket, idempotency_key: str
    ) -> DisputeId:
        return await self._write_with_retry(self._submit_dispute_ticket, ticket, idempotency_key)

    def _submit_dispute_ticket(self, ticket: DisputeTicket, idempotency_key: str) -> DisputeId:
        now = self._clock.now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT INTO dispute_tickets (id, order_id, delivery_id, customer_id, dispute_type,"
                    " status, reported_at, sla_resolution_deadline, investigation_notes_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        ticket.dispute_id,
                        ticket.order_id,
                        ticket.delivery_id,
                        ticket.customer_id,
                        ticket.dispute_type.value,
                        ticket.status.value,
                        to_utc_iso(ticket.reported_at),
                        to_utc_iso(ticket.sla_resolution_deadline),
                        json.dumps(ticket.investigation_notes),
                    ),
                )
                self._append_event(
                    now,
                    "dispute",
                    ticket.dispute_id,
                    "dispute_opened",
                    ticket.model_dump_json(),
                    idempotency_key,
                )
                self._conn.execute("COMMIT")
                return ticket.dispute_id
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    async def record_order_feedback(
        self, *, feedback: OrderFeedback, idempotency_key: str
    ) -> None:
        await self._write_with_retry(self._record_order_feedback, feedback, idempotency_key)

    def _record_order_feedback(self, feedback: OrderFeedback, idempotency_key: str) -> None:
        now = self._clock.now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT INTO order_feedback (id, order_id, customer_id, target_type, target_item_id,"
                    " rating, category_tags_json, spoken_comment, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        feedback.feedback_id,
                        feedback.order_id,
                        feedback.customer_id,
                        feedback.target_type.value,
                        feedback.target_item_id,
                        feedback.rating,
                        json.dumps([t.value for t in feedback.category_tags]),
                        feedback.spoken_comment,
                        to_utc_iso(feedback.created_at),
                    ),
                )
                self._append_event(
                    now,
                    "feedback",
                    feedback.feedback_id,
                    "feedback_recorded",
                    feedback.model_dump_json(),
                    idempotency_key,
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    # ------------------------------------------------------- Internals
    def _return_requests_for_item_locked(self, order_item_id: OrderItemId) -> list[ReturnRequest]:
        rows = self._conn.execute(
            "SELECT rr.*, oi.order_id FROM return_requests rr "
            "JOIN order_items oi ON oi.id = rr.order_item_id "
            "WHERE rr.order_item_id = ? ORDER BY rr.created_at",
            (order_item_id,),
        ).fetchall()
        return [_return_from_row(r) for r in rows]

    def _has_exchangeable_variants_locked(
        self, product_id: int, current_variant_id: VariantId
    ) -> bool:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM product_variants WHERE product_id = ? AND id != ? AND in_stock = 1",
            (product_id, current_variant_id),
        ).fetchone()
        return bool(row and row[0] > 0)

    def _replay(self, idempotency_key: str) -> str | None:
        row = self._conn.execute(
            "SELECT payload_json FROM domain_events WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        return row["payload_json"] if row else None

    def _append_event(
        self,
        now: datetime,
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
