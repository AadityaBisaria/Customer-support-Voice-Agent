"""The confirm gate: readback -> deterministic yes/no -> execute in code.

`make_confirm` arms a YesNoSlot gate and returns a TOOLLESS node whose only
job is to speak the templated readback. The decision callback — never the
model — executes the SupportStore mutation with the pre-minted idempotency
key and moves to a done/cancelled/failed node built from the actual result.
"""

from loguru import logger

from dialogue.yes_no import YesNoSlot
from store.domain import OrderId, OrderItemId, RefundInfo, RefundMethod, ReturnReason
from store.policy import PolicyError

from .gate_processor import GATE_STATE_KEY, SlotGate
from .pending import PendingMutation, speak_date, speak_money
from .session import SessionDeps

_YES_NO = YesNoSlot(name="confirmed", prompt="haan ya nahi?")


def _refund_sentence(refund: RefundInfo | None) -> str:
    if refund is None:
        return ""
    return (
        f" A refund of {speak_money(refund.amount)} to {refund.method.value} is expected "
        f"by {speak_date(refund.expected_by)}."
    )


async def make_confirm(deps: SessionDeps, flow_manager, pending: PendingMutation) -> dict:
    from .nodes import task_messages  # local import: nodes imports this module

    flow_manager.state[GATE_STATE_KEY] = SlotGate(
        slot=_YES_NO,
        on_fit=lambda yes: _on_decision(deps, flow_manager, pending, yes),
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


async def _on_decision(
    deps: SessionDeps, flow_manager, pending: PendingMutation, yes: bool
) -> None:
    from .nodes import make_wrap  # local import: nodes imports this module

    if not yes:
        logger.info("confirm gate: declined {} ({})", pending.op, pending.idempotency_key)
        node = await make_wrap(
            deps,
            flow_manager,
            "Tell the user: no problem, nothing was changed. Ask if they need anything else.",
            name="mutation_cancelled",
        )
        await flow_manager.set_node_from_config(node)
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
                RefundMethod(pending.args["refund_destination"])
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
