"""Readback templates and PendingMutation basics."""

from datetime import datetime

from flows.pending import (
    PendingMutation,
    cancel_readback,
    refund_line_for,
    replacement_readback,
    return_readback,
    speak_date,
    speak_money,
)
from language import Band
from store.clock import IST
from store.domain import Money

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=IST)


def test_idempotency_keys_are_unique_and_preminted():
    a = PendingMutation(op="cancel_order", args={}, readback="x")
    b = PendingMutation(op="cancel_order", args={}, readback="x")
    assert a.idempotency_key and a.idempotency_key != b.idempotency_key


def test_speak_helpers():
    assert speak_date(NOW) == "2 September"
    assert speak_money(Money.rupees(1299)) == "1299 rupees"


def test_cancel_readback_carries_facts_in_every_band():
    line = refund_line_for(
        Band.HINGLISH, amount=Money.rupees(399), method="amazon_pay", expected=NOW
    )
    for band in Band:
        text = cancel_readback(band, order_id="AMZ-1004", titles="phone case", refund_line=line)
        assert "AMZ-1004" in text and "phone case" in text


def test_hindi_bands_use_devanagari():
    text = cancel_readback(Band.MOSTLY_HINDI, order_id="AMZ-1", titles="x", refund_line="")
    assert any("ऀ" <= ch <= "ॿ" for ch in text)
    english = cancel_readback(Band.MOSTLY_ENGLISH, order_id="AMZ-1", titles="x", refund_line="")
    assert not any("ऀ" <= ch <= "ॿ" for ch in english)


def test_return_and_replacement_readbacks_ask_for_consent():
    for band in Band:
        assert "?" in return_readback(band, title="shoes", reason="size issue", refund_line="")
        assert "?" in replacement_readback(band, title="shoes", reason="damaged")
