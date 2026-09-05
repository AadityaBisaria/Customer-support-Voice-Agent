"""Deterministic demo fixtures. Dates are offsets from the given `now`, so
return-window math stays valid forever. Every policy branch is reachable:

  Priya   9876543210  AMZ-1001 kurta set (apparel, returnable_30d, card, delivered -5d, exchangeable)
                      AMZ-1002 running shoes (returnable_10d, upi, delivered -3d, in stock)
                      AMZ-1003 Echo Dot (replacement_only_7d, card, delivered -2d)
                      AMZ-1004 phone case (returnable_10d, upi, PLACED -> cancel eligible)
  Rahul   9123456789  AMZ-2001 mixer (returnable_10d, cash_on_delivery, delivered -4d -> COD refund)
                      AMZ-2002 headphones (replacement_only_10d, card, SHIPPED -> delivery reschedule eligible)
                      AMZ-2003 t-shirt (returnable_30d, cash_on_delivery, PLACED -> cancel, doorstep QR convert)
  Anita   9000000001  AMZ-3001 sandals (returnable_10d, netbanking, delivered -25d -> window closed)
                      AMZ-3002 speaker (replacement_only_7d, upi, delivered -3d, ALREADY REPLACED once)
                      AMZ-3003 desk lamp (returnable_10d, card, delivered -2d, variant OUT OF STOCK)
                      AMZ-3004 wax candles (non_returnable, upi, delivered -1d -> damage-class only)
                      AMZ-3005 backpack (returnable_30d, card, delivered -10d, completed return + REF-2001)
                      AMZ-3006 smartwatch (delivered -1d -> eligible for fake/missing delivery dispute)
  Vikram  9111111111  no orders (empty-result branch)
"""

import json
import sqlite3
from datetime import datetime, timedelta

from .clock import to_utc_iso


def seed(conn: sqlite3.Connection, now: datetime) -> None:
    def days(n: float) -> str:
        return to_utc_iso(now + timedelta(days=n))

    conn.execute("BEGIN")

    # 1. Customers
    conn.executemany(
        "INSERT INTO customers (id, name, phone, email) VALUES (?,?,?,?)",
        [
            (1, "Priya Sharma", "9876543210", "priya@example.com"),
            (2, "Rahul Verma", "9123456789", "rahul@example.com"),
            (3, "Anita Desai", "9000000001", "anita@example.com"),
            (4, "Vikram Singh", "9111111111", "vikram@example.com"),
        ],
    )

    # 2. Customer Accounts (verification state & saved refund destinations)
    conn.executemany(
        "INSERT INTO customer_accounts (account_id, customer_id, status, verified_channels_json,"
        " auth_failure_count, default_refund_destination_json) VALUES (?,?,?,?,?,?)",
        [
            (
                1,
                1,
                "active",
                json.dumps(["phone", "email"]),
                0,
                json.dumps({"kind": "upi", "upi_id": "priya@okhdfcbank"}),
            ),
            (
                2,
                2,
                "active",
                json.dumps(["phone"]),
                0,
                json.dumps(
                    {
                        "kind": "neft",
                        "account_holder_name": "Rahul Verma",
                        "account_number": "10029938821",
                        "ifsc": "SBIN0001234",
                    }
                ),
            ),
            (
                3,
                3,
                "active",
                json.dumps(["phone"]),
                0,
                None,
            ),
            (
                4,
                4,
                "active",
                json.dumps(["phone"]),
                0,
                None,
            ),
        ],
    )

    # 3. Addresses
    conn.executemany(
        "INSERT INTO addresses (id, customer_id, label, line1, city, pincode) VALUES (?,?,?,?,?,?)",
        [
            (1, 1, "home", "12 MG Road", "Bengaluru", "560001"),
            (2, 2, "home", "45 Linking Road", "Mumbai", "400050"),
            (3, 3, "home", "8 Park Street", "Kolkata", "700016"),
            (4, 4, "home", "2 Anna Salai", "Chennai", "600002"),
        ],
    )

    # 4. Products: (id, title, category, price_paise, return_policy_type)
    conn.executemany(
        "INSERT INTO products (id, title, category, price_paise, return_policy_type) VALUES (?,?,?,?,?)",
        [
            (1, "Anarkali kurta set", "apparel", 189900, "returnable_30d"),
            (2, "cotton dupatta", "apparel", 49900, "returnable_30d"),
            (3, "running shoes", "footwear", 259900, "returnable_10d"),
            (4, "Echo Dot 5th Gen", "electronics", 449900, "replacement_only_7d"),
            (5, "phone case", "accessories", 39900, "returnable_10d"),
            (6, "mixer grinder", "appliances", 329900, "returnable_10d"),
            (7, "wireless headphones", "electronics", 199900, "replacement_only_10d"),
            (8, "printed t-shirt", "apparel", 59900, "returnable_30d"),
            (9, "leather sandals", "footwear", 149900, "returnable_10d"),
            (10, "bluetooth speaker", "electronics", 249900, "replacement_only_7d"),
            (11, "study desk lamp", "home", 89900, "returnable_10d"),
            (12, "scented wax candles", "home", 44900, "non_returnable"),
            (13, "laptop backpack", "accessories", 179900, "returnable_30d"),
            (14, "Smart Fitness Watch", "electronics", 299900, "returnable_10d"),
        ],
    )

    # 5. Product Variants: (id, product_id, size, color, in_stock)
    # Includes sibling sizes for exchange workflows on Product 1 & 8
    conn.executemany(
        "INSERT INTO product_variants (id, product_id, size, color, in_stock) VALUES (?,?,?,?,?)",
        [
            # Product 1 (Kurta): M/teal purchased; S and L available for exchange
            (1, 1, "M", "teal", 1),
            (101, 1, "S", "teal", 1),
            (102, 1, "L", "teal", 1),
            (103, 1, "M", "maroon", 0),  # Out of stock variant test
            # Remaining products
            (2, 2, None, "gold", 1),
            (3, 3, "UK 9", "black", 1),
            (4, 4, None, "charcoal", 1),
            (5, 5, None, "clear", 1),
            (6, 6, None, "white", 1),
            (7, 7, None, "black", 1),
            # Product 8 (T-Shirt): L/navy purchased; XL available
            (8, 8, "L", "navy", 1),
            (104, 8, "XL", "navy", 1),
            (9, 9, "UK 6", "tan", 1),
            (10, 10, None, "blue", 1),
            (11, 11, None, "white", 0),  # OUT OF STOCK -> replacement unavailable
            (12, 12, None, None, 1),
            (13, 13, None, "grey", 1),
            (14, 14, None, "black", 1),
        ],
    )

    # 6. Orders: (id, customer, address, status, payment, placed, shipped, delivered, total, mode, link)
    conn.executemany(
        "INSERT INTO orders (id, customer_id, address_id, status, payment_method,"
        " placed_at, shipped_at, delivered_at, total_paise, payment_collection_mode, pre_delivery_payment_link) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("AMZ-1001", 1, 1, "delivered", "card", days(-8), days(-6), days(-5), 239800, None, None),
            ("AMZ-1002", 1, 1, "delivered", "upi", days(-6), days(-4), days(-3), 259900, None, None),
            ("AMZ-1003", 1, 1, "delivered", "card", days(-5), days(-3), days(-2), 449900, None, None),
            ("AMZ-1004", 1, 1, "placed", "upi", days(-0.5), None, None, 39900, None, None),
            ("AMZ-2001", 2, 2, "delivered", "cash_on_delivery", days(-7), days(-5), days(-4), 329900, "cash", None),
            ("AMZ-2002", 2, 2, "shipped", "card", days(-2), days(-1), None, 199900, None, None),
            ("AMZ-2003", 2, 2, "placed", "cash_on_delivery", days(-0.2), None, None, 59900, "cash", "https://pay.example.in/amz-2003"),
            ("AMZ-3001", 3, 3, "delivered", "netbanking", days(-28), days(-26), days(-25), 149900, None, None),
            ("AMZ-3002", 3, 3, "delivered", "upi", days(-6), days(-4), days(-3), 249900, None, None),
            ("AMZ-3003", 3, 3, "delivered", "card", days(-4), days(-3), days(-2), 89900, None, None),
            ("AMZ-3004", 3, 3, "delivered", "upi", days(-3), days(-2), days(-1), 44900, None, None),
            ("AMZ-3005", 3, 3, "delivered", "card", days(-13), days(-11), days(-10), 179900, None, None),
            ("AMZ-3006", 3, 3, "delivered", "card", days(-2), days(-1.5), days(-1), 299900, None, None),
        ],
    )

    # 7. Deliveries (shipment tracking and rescheduling fixture)
    conn.executemany(
        "INSERT INTO deliveries (id, order_id, courier_partner, tracking_number, status,"
        " estimated_delivery_date, actual_delivery_date, rescheduled_delivery_date, reschedule_count,"
        " delivery_slot_preference, delivery_instructions, latest_checkpoint_text, failed_attempt_reason) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                1,
                "AMZ-1001",
                "BlueDart",
                "BD-982104",
                "delivered",
                days(-5),
                days(-5),
                None,
                0,
                None,
                "Leave with security",
                "Package delivered to recipient",
                None,
            ),
            (
                2,
                "AMZ-2002",
                "Delhivery",
                "DEL-881230",
                "in_transit",
                days(1),
                None,
                None,
                0,
                "morning",
                None,
                "Arrived at Bengaluru Sort Facility",
                None,
            ),
            (
                3,
                "AMZ-3006",
                "Shadowfax",
                "SF-441092",
                "delivered",
                days(-1),
                days(-1),
                None,
                0,
                None,
                None,
                "Delivered at front door with OTP confirmation",
                None,
            ),
        ],
    )

    # 8. Order Items
    conn.executemany(
        "INSERT INTO order_items (id, order_id, variant_id, quantity, price_paise) VALUES (?,?,?,?,?)",
        [
            (1, "AMZ-1001", 1, 1, 189900),
            (2, "AMZ-1001", 2, 1, 49900),
            (3, "AMZ-1002", 3, 1, 259900),
            (4, "AMZ-1003", 4, 1, 449900),
            (5, "AMZ-1004", 5, 1, 39900),
            (6, "AMZ-2001", 6, 1, 329900),
            (7, "AMZ-2002", 7, 1, 199900),
            (8, "AMZ-2003", 8, 1, 59900),
            (9, "AMZ-3001", 9, 1, 149900),
            (10, "AMZ-3002", 10, 1, 249900),
            (11, "AMZ-3003", 11, 1, 89900),
            (12, "AMZ-3004", 12, 1, 44900),
            (13, "AMZ-3005", 13, 1, 179900),
            (14, "AMZ-3006", 14, 1, 299900),
        ],
    )

    # 9. Existing Returns & Refunds (Anita's historical branches)
    conn.execute(
        "INSERT INTO return_requests (id, order_item_id, resolution, reason, status,"
        " replacement_variant_id, created_at) VALUES "
        "('RET-0901', 10, 'replacement', 'defective', 'completed', 10, ?)",
        (days(-2),),
    )
    conn.execute(
        "INSERT INTO return_requests (id, order_item_id, resolution, reason, status,"
        " replacement_variant_id, created_at) VALUES "
        "('RET-0902', 13, 'refund', 'not_needed', 'completed', NULL, ?)",
        (days(-3),),
    )
    conn.execute(
        "INSERT INTO refunds (id, order_id, return_request_id, origin, amount_paise,"
        " method, status, initiated_at, expected_by) VALUES "
        "('REF-2001', 'AMZ-3005', 'RET-0902', 'return', 179900, 'card', 'initiated', ?, ?)",
        (days(-1), days(6)),
    )

    # 10. Initial Seed Event Log
    conn.execute(
        "INSERT INTO domain_events (occurred_at, aggregate_type, aggregate_id, event_type,"
        " payload_json, idempotency_key) VALUES (?,?,?,?,?,NULL)",
        (to_utc_iso(now), "system", "seed", "seeded", "{}"),
    )
    conn.execute("COMMIT")