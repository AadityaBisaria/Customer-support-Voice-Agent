"""PendingMutation and the speech-facing rendering of amounts, dates, and
readbacks. The readback is templated in code — the model is told to speak it,
never to compose it — and rendered per the caller's current language band.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from language import Band
from store.clock import IST
from store.domain import Money

_MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]


def speak_date(dt: datetime) -> str:
    """'9 September' in IST — the only place persistence-UTC becomes speech."""
    local = dt.astimezone(IST)
    return f"{local.day} {_MONTHS[local.month - 1]}"


def speak_money(amount: Money) -> str:
    return amount.speak()


MutationOp = Literal["cancel_order", "create_return", "create_replacement"]


@dataclass(frozen=True)
class PendingMutation:
    """Everything the confirm gate needs: what to do, and the exact words.

    The idempotency key is minted here — before the caller says yes — so a
    replayed confirmation (barge-in, reconnect) executes exactly once.
    """

    op: MutationOp
    args: dict[str, Any]
    readback: str
    idempotency_key: str = field(default_factory=lambda: str(uuid.uuid4()))


def _banded(band: Band, english: str, hinglish: str, hindi: str) -> str:
    if band is Band.MOSTLY_ENGLISH:
        return english
    if band is Band.MOSTLY_HINDI:
        return hindi
    return hinglish


def cancel_readback(band: Band, *, order_id: str, titles: str, refund_line: str) -> str:
    return _banded(
        band,
        f"Cancelling order {order_id}, {titles}.{refund_line} Should I go ahead?",
        f"Order {order_id}, {titles} — cancel कर दूं?{refund_line} हां या नहीं बोलिए.",
        f"Order {order_id}, {titles} — कैंसिल कर दूं?{refund_line} हां या नहीं बोलिए.",
    )


def refund_line_for(band: Band, *, amount: Money, method: str, expected: datetime) -> str:
    spoken_method = method.replace("_", " ")
    return _banded(
        band,
        f" Your refund of {speak_money(amount)} goes to {spoken_method}, expected by {speak_date(expected)}.",
        f" Refund {speak_money(amount)} aapke {spoken_method} में आएगा, {speak_date(expected)} तक.",
        f" {speak_money(amount)} का refund आपके {spoken_method} में {speak_date(expected)} तक आ जाएगा.",
    )


def return_readback(band: Band, *, title: str, reason: str, refund_line: str) -> str:
    return _banded(
        band,
        f"Creating a return for the {title}, reason {reason}.{refund_line} Should I go ahead?",
        f"{title} का return बना दूं, reason {reason}?{refund_line} हां या नहीं बोलिए.",
        f"{title} का return बना दूं, कारण {reason}?{refund_line} हां या नहीं बोलिए.",
    )


def replacement_readback(band: Band, *, title: str, reason: str) -> str:
    return _banded(
        band,
        f"Arranging a free replacement for the {title}, reason {reason}. Should I go ahead?",
        f"{title} का free replacement arrange कर दूं, reason {reason}? हां या नहीं बोलिए.",
        f"{title} का मुफ़्त replacement arrange कर दूं, कारण {reason}? हां या नहीं बोलिए.",
    )
