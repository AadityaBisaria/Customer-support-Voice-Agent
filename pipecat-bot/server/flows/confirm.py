"""The confirm gate: readback -> deterministic yes/no -> execute in code.

`make_confirm` arms a YesNoSlot gate and returns a TOOLLESS node whose only
job is to speak the templated readback. The decision callback — never the
model — executes the SupportStore mutation with the pre-minted idempotency
key and moves to a done/cancelled/failed node built from the actual result.
"""

from datetime import datetime, timedelta
from uuid import uuid4

from loguru import logger
from pydantic import TypeAdapter

from dialogue.yes_no import YesNoSlot
from store.domain import (
    DeliveryId,
    DeliverySlotPreference,
    DisputeId,
    DisputeTicket,
    DisputeType,
    OrderId,
    OrderItemId,
    RefundDestination,
    RefundInfo,
    ReturnReason,
    VariantId,
)
from store.policy import PolicyError

from .gate_processor import GATE_STATE_KEY, SlotGate
from .pending import PendingMutation, speak_date, speak_money
from .session import SessionDeps

_YES_NO = YesNoSlot(name="confirmed", prompt="haan ya nahi?")
PENDING_STATE_KEY = "pending"


def _refund_sentence(refund: RefundInfo | None) -> str:
    if refund is None:
        return ""
    return (
        f" A refund of {speak_money(refund.amount)} to {refund.method.value} is expected "
        f"by {speak_date(refund.expected_by)}."
    )


async def make_confirm(deps: SessionDeps, flow_manager, pending: PendingMutation) -> dict:
    from .nodes import task_messages  # local import: nodes imports this module

    # The pending operation is durable flow state, not an LLM tool argument.
    # The gate callback reads it back only after YesNoSlot yields an unambiguous fit.
    flow_manager.state[PENDING_STATE_KEY] = pending
    flow_manager.state[GATE_STATE_KEY] = SlotGate(
        slot=_YES_NO,
        on_fit=lambda yes: _on_decision(deps, flow_manager, yes),
    )
    return {
        "name": "confirm_mutation",
        "task_messages": task_messages(
            deps,
            "Read back exactly this to the user and ask them to confirm: "
            f"'{pending.readback}'. If their answer was not a clear yes or no, "
            "briefly ask again for a clear haan or nahi. Do nothing else.",
        ),
        "functions": [],  # the LLM can do nothing here but speak
    }


async def _on_decision(deps: SessionDeps, flow_manager, yes: bool) -> None:
    from .nodes import make_wrap  # local import: nodes imports this module

    pending = flow_manager.state.get(PENDING_STATE_KEY)
    if not isinstance(pending, PendingMutation):
        logger.error("confirm gate had no pending mutation")
        node = await make_wrap(
            deps,
            flow_manager,
            "Apologize: that request expired before it could be completed. Nothing was changed.",
            name="mutation_failed",
        )
        await flow_manager.set_node_from_config(node)
        return

    if not yes:
        logger.info("confirm gate: declined {} ({})", pending.op, pending.idempotency_key)
        node = await make_wrap(
            deps,
            flow_manager,
            "Tell the user: no problem, nothing was changed. Ask if they need anything else.",
            name="mutation_cancelled",
        )
        await flow_manager.set_node_from_config(node)
        flow_manager.state.pop(PENDING_STATE_KEY, None)
        return

    logger.info("confirm gate: executing {} ({})", pending.op, pending.idempotency_key)
    try:
        text = await _execute(deps, pending)
        node = await make_wrap(deps, flow_manager, text, name="mutation_done")
    except PolicyError as e:
        node = await make_wrap(
            deps,
            flow_manager,
            f"Tell the user this couldn't be done: {e.message} Nothing was changed. "
            "Ask if they need anything else.",
            name="mutation_failed",
        )
    except Exception:
        logger.exception("mutation {} failed", pending.op)
        node = await make_wrap(
            deps,
            flow_manager,
            "Apologize: something went wrong on our side and nothing was changed. "
            "Ask them to try again in a moment.",
            name="mutation_failed",
        )
    await flow_manager.set_node_from_config(node)
    flow_manager.state.pop(PENDING_STATE_KEY, None)


async def _execute(deps: SessionDeps, pending: PendingMutation) -> str:
    """Run the mutation; render the done-node instruction from the REAL result."""
    if pending.op == "cancel_order":
        result = await deps.store.cancel_order(
            order_id=OrderId(pending.args["order_id"]),
            idempotency_key=pending.idempotency_key,
        )
        text = f"Order {result.order_id} is cancelled.{_refund_sentence(result.refund)}"
        if result.refund is None:
            text += " Since it was Cash on Delivery, no payment was taken."
        return f"Tell the user: {text} Ask if they need anything else."

    if pending.op == "create_return":
        result = await deps.store.create_return(
            order_item_id=OrderItemId(pending.args["order_item_id"]),
            reason=ReturnReason(pending.args["reason"]),
            refund_destination=(
                TypeAdapter(RefundDestination).validate_python(pending.args["refund_destination"])
                if pending.args.get("refund_destination")
                else None
            ),
            idempotency_key=pending.idempotency_key,
        )
        return (
            f"Tell the user: the return is created, reference {result.return_id}. Pickup will "
            f"happen by {speak_date(result.pickup_by)}.{_refund_sentence(result.refund)} "
            "Ask if they need anything else."
        )

    if pending.op == "create_replacement":
        result = await deps.store.create_replacement(
            order_item_id=OrderItemId(pending.args["order_item_id"]),
            reason=ReturnReason(pending.args["reason"]),
            idempotency_key=pending.idempotency_key,
        )
        return (
            f"Tell the user: the free replacement is arranged, reference {result.return_id}. The "
            f"original item will be picked up by {speak_date(result.pickup_by)}, and the replacement "
            "ships once it's collected. Ask if they need anything else."
        )

    if pending.op == "reschedule_delivery":
        delivery = await deps.store.reschedule_delivery(
            delivery_id=DeliveryId(pending.args["delivery_id"]),
            new_date=datetime.fromisoformat(pending.args["new_date"]),
            slot=(
                DeliverySlotPreference(pending.args["slot"])
                if pending.args.get("slot")
                else None
            ),
            instructions=pending.args.get("instructions"),
            idempotency_key=pending.idempotency_key,
        )
        return (
            f"Tell the user: the delivery is rescheduled to "
            f"{speak_date(delivery.rescheduled_delivery_date)}. Ask if they need anything else."
        )

    if pending.op == "submit_dispute_ticket":
        reported_at = deps.clock.now()
        ticket = DisputeTicket(
            dispute_id=DisputeId(f"DSP-{uuid4().hex[:10].upper()}"),
            order_id=OrderId(pending.args["order_id"]),
            delivery_id=(
                DeliveryId(pending.args["delivery_id"])
                if pending.args.get("delivery_id") is not None
                else None
            ),
            customer_id=pending.args["customer_id"],
            dispute_type=DisputeType(pending.args["dispute_type"]),
            reported_at=reported_at,
            sla_resolution_deadline=reported_at + timedelta(hours=48),
        )
        dispute_id = await deps.store.submit_dispute_ticket(
            ticket=ticket, idempotency_key=pending.idempotency_key
        )
        return (
            f"Tell the user: the delivery investigation is opened, reference {dispute_id}. "
            "The logistics team will review it within 48 hours. Ask if they need anything else."
        )

    result = await deps.store.create_exchange(
        order_item_id=OrderItemId(pending.args["order_item_id"]),
        new_variant_id=VariantId(pending.args["new_variant_id"]),
        reason=ReturnReason(pending.args["reason"]),
        idempotency_key=pending.idempotency_key,
    )
    return (
        f"Tell the user: the exchange is arranged, reference {result.return_id}. The original item "
        f"will be picked up by {speak_date(result.pickup_by)}, and the requested size or color ships "
        "once it is collected. Ask if they need anything else."
    )
