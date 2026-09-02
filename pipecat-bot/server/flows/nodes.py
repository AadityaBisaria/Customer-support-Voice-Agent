"""The flow graph: node factories and per-node tool menus.

Invariants (test-enforced by flows/tests/test_graph.py):
- routing tools carry no data (empty properties);
- selection enums are built from LIVE store reads — a hallucinated order id
  is structurally unrepresentable;
- the mutation methods (cancel_order / create_return / create_replacement)
  appear in NO functions list anywhere — only the confirm gate calls them;
- gate nodes (verify_phone, confirm_mutation) are toolless;
- no node sets context_strategy (RESET would delete the [LANG-STYLE] directive).
"""

from typing import Literal

from loguru import logger
from pipecat.flows import FlowsFunctionSchema

from dialogue.phone import PhoneSlot
from language import short_hint_for_band
from store.domain import (
    OrderId,
    OrderStatus,
    OrderSummary,
    PaymentMethod,
    PhoneNumber,
    RefundMethod,
    Resolution,
    ReturnReason,
)
from store.policy import PolicyError, refund_expectation, valid_resolutions

from .confirm import make_confirm
from .gate_processor import GATE_STATE_KEY, SlotGate
from .pending import (
    PendingMutation,
    cancel_readback,
    refund_line_for,
    replacement_readback,
    return_readback,
    speak_date,
    speak_money,
)
from .session import SessionDeps

Purpose = Literal["status", "return", "cancel"]

_PHONE = PhoneSlot(name="phone", prompt="Apna registered mobile number boliye")

_REASON_SPOKEN = {
    ReturnReason.DAMAGED: "it arrived damaged",
    ReturnReason.DEFECTIVE: "it is defective",
    ReturnReason.WRONG_ITEM: "a wrong item arrived",
    ReturnReason.MISSING_PARTS: "parts are missing",
    ReturnReason.NOT_NEEDED: "no longer needed",
    ReturnReason.SIZE_ISSUE: "size or fit issue",
}


def task_messages(deps: SessionDeps, text: str) -> list[dict]:
    """One developer task message, always closing with the language hint."""
    hint = short_hint_for_band(deps.tracker.band)
    return [{"role": "developer", "content": f"{text}\nReply language: {hint}"}]


def _deps(flow_manager) -> SessionDeps:
    return flow_manager.state["deps"]


# ------------------------------------------------------------ shared tools


def _routing(name: str, description: str, factory) -> FlowsFunctionSchema:
    """A routing tool: pure navigation, no data rides on it."""

    async def handler(args, flow_manager):
        return None, await factory(_deps(flow_manager), flow_manager)

    return FlowsFunctionSchema(
        name=name, description=description, properties={}, required=[], handler=handler
    )


def _menu_tools() -> list[FlowsFunctionSchema]:
    """The four transactional entry points, offered wherever routing happens."""
    return [
        _routing(
            "start_order_status",
            "The user asks where an order is or wants a delivery update.",
            lambda d, fm: make_select_order(d, fm, "status"),
        ),
        _routing(
            "start_return_or_exchange",
            "The user wants to return, exchange, or replace something they received.",
            lambda d, fm: make_select_order(d, fm, "return"),
        ),
        _routing(
            "start_refund_status",
            "The user asks where their refund is or when money comes back.",
            make_refund_status_report,
        ),
        _routing(
            "start_cancel_order",
            "The user wants to cancel an order they placed.",
            lambda d, fm: make_select_order(d, fm, "cancel"),
        ),
    ]


def _wrap_tools() -> list[FlowsFunctionSchema]:
    """Wrap/report nodes route directly — one hop per intent, no menu detour."""
    return [
        *_menu_tools(),
        _routing("end_call", "The user is done and says goodbye.", make_end),
    ]


# ------------------------------------------------------------------ nodes


async def make_greet_unauth(deps: SessionDeps, flow_manager) -> dict:
    """Initial node for webrtc/eval (and twilio callers with no DB match).

    Behaviorally the pre-flows bot — greeting + KB-grounded Q&A — plus one
    routing tool. This is what keeps the milestone-1 eval suites green.
    """
    return {
        "name": "greet_unauth",
        "task_messages": task_messages(
            deps,
            "Greet the caller briefly: you are a demo support assistant for Amazon "
            "returns, refunds, replacements, and delivery questions. Answer general "
            "policy questions directly. The moment they mention THEIR order or "
            "account in any way — my order, mera order, a return, a refund, a "
            "cancellation — call start_order_help RIGHT AWAY. Never ask permission "
            "to use it, never announce it, never mention tools: function calls are "
            "invisible to the caller.",
        ),
        "functions": [
            _routing(
                "start_order_help",
                "Call immediately (without asking) when the user mentions their "
                "order or account: status, return, exchange, replacement, refund, "
                "or cancellation.",
                _order_help_entry,
            )
        ],
    }


async def _order_help_entry(deps: SessionDeps, flow_manager) -> dict:
    if deps.customer is not None:
        return await make_triage(deps, flow_manager, greet=True)
    return await make_verify_phone(deps, flow_manager)


async def make_verify_phone(deps: SessionDeps, flow_manager, *, note: str | None = None) -> dict:
    """Toolless; a PhoneSlot gate captures the number deterministically."""

    async def on_fit(digits: str) -> None:
        customer = await deps.store.customer_by_phone(PhoneNumber(digits))
        if customer is not None:
            deps.customer = customer
            deps.verify_attempts = 0
            logger.info("verified caller {} via phone", customer.name)
            await flow_manager.set_node_from_config(
                await make_triage(deps, flow_manager, greet=True)
            )
            return
        deps.verify_attempts += 1
        if deps.verify_attempts >= 2:
            await flow_manager.set_node_from_config(await make_kb_only(deps, flow_manager))
            return
        await flow_manager.set_node_from_config(
            await make_verify_phone(
                deps,
                flow_manager,
                note="That number didn't match an account.",
            )
        )

    async def on_exhausted() -> None:
        await flow_manager.set_node_from_config(await make_kb_only(deps, flow_manager))

    flow_manager.state[GATE_STATE_KEY] = SlotGate(
        slot=_PHONE, on_fit=on_fit, on_exhausted=on_exhausted, max_attempts=3
    )
    prefix = f"{note} " if note else ""
    return {
        "name": "verify_phone",
        "task_messages": task_messages(
            deps,
            f"{prefix}Ask the caller to speak their 10-digit registered mobile "
            "number, digit by digit. If what they said wasn't a number, ask again.",
        ),
        "functions": [],
    }


async def make_kb_only(deps: SessionDeps, flow_manager) -> dict:
    return {
        "name": "kb_only",
        "task_messages": task_messages(
            deps,
            "The caller could not be verified. Apologize briefly; say you can still "
            "answer general questions about returns, refunds, replacements, and "
            "delivery policies, but account-specific help needs a call from the "
            "registered number. Answer their policy questions.",
        ),
        "functions": [],
    }


async def make_triage(deps: SessionDeps, flow_manager, *, greet: bool = False) -> dict:
    deps.wip.clear()
    assert deps.customer is not None
    greeting = (
        f"Greet the caller by name — they are {deps.customer.name} — and ask how you can help. "
        if greet
        else ""
    )
    return {
        "name": "triage",
        "task_messages": task_messages(
            deps,
            f"{greeting}You can check order status, start a return or exchange, "
            "check a refund, or cancel an unshipped order — call the matching "
            "function when the user asks. You can also answer general policy "
            "questions directly.",
        ),
        "functions": _menu_tools(),
    }


_PURPOSE_FILTER: dict[Purpose, set[OrderStatus] | None] = {
    "status": None,
    "return": {OrderStatus.DELIVERED},
    "cancel": {OrderStatus.PLACED},
}

_PURPOSE_EMPTY = {
    "status": "Tell the user: there are no orders on this account.",
    "return": "Tell the user: there are no delivered orders that could be returned.",
    "cancel": (
        "Tell the user: there are no orders that can still be cancelled — "
        "cancellation is only possible before an order ships."
    ),
}


def _order_line(index: int, order: OrderSummary) -> str:
    titles = ", ".join(order.item_titles)
    when = speak_date(order.delivered_at) if order.delivered_at else speak_date(order.placed_at)
    verb = "delivered" if order.delivered_at else "placed"
    return f"{index}. {order.id}: {titles} ({verb} {when}, {speak_money(order.total)})"


async def make_select_order(deps: SessionDeps, flow_manager, purpose: Purpose) -> dict:
    statuses = _PURPOSE_FILTER[purpose]
    assert deps.customer is not None
    orders = await deps.store.orders_for_customer(deps.customer.id, statuses=statuses)
    if not orders:
        return await make_nothing_here(deps, flow_manager, _PURPOSE_EMPTY[purpose])

    deps.wip["purpose"] = purpose
    listing = "\n".join(_order_line(i + 1, o) for i, o in enumerate(orders))
    valid_ids = [str(o.id) for o in orders]

    async def handler(args, flow_manager):
        order_id = args["order_id"]
        if order_id not in valid_ids:  # never trust the model past the enum
            return {"error": "unknown order"}, None
        d = _deps(flow_manager)
        d.wip["order_id"] = order_id
        return None, await _after_order_selected(d, flow_manager, purpose, OrderId(order_id))

    select = FlowsFunctionSchema(
        name="select_order",
        description="Record which of the listed orders the user means.",
        properties={"order_id": {"type": "string", "enum": valid_ids}},
        required=["order_id"],
        handler=handler,
    )
    return {
        "name": f"select_order_{purpose}",
        "task_messages": task_messages(
            deps,
            "Ask which order the user means, offering these (they may answer by "
            f"position, product, or date):\n{listing}\n"
            "Call select_order with the matching order id. If they change their "
            "mind, call go_back.",
        ),
        "functions": [select, _routing("go_back", "The user wants the main menu.", make_triage)],
    }


async def _after_order_selected(
    deps: SessionDeps, flow_manager, purpose: Purpose, order_id: OrderId
) -> dict:
    if purpose == "status":
        return await make_order_status_report(deps, flow_manager, order_id)
    if purpose == "cancel":
        return await _build_cancel_confirm(deps, flow_manager, order_id)
    items = await deps.store.items_for_order(order_id)
    if len(items) == 1:
        deps.wip["order_item_id"] = int(items[0].item.id)
        deps.wip["item_title"] = items[0].product.title
        return await make_select_reason(deps, flow_manager)
    return await make_select_item(deps, flow_manager, order_id)


async def make_order_status_report(deps: SessionDeps, flow_manager, order_id: OrderId) -> dict:
    order = await deps.store.order_with_items(order_id)
    items = await deps.store.items_for_order(order_id)
    titles = ", ".join(d.product.title for d in items)
    if order.status is OrderStatus.DELIVERED:
        line = f"was delivered on {speak_date(order.delivered_at)}"
    elif order.status in (OrderStatus.SHIPPED, OrderStatus.OUT_FOR_DELIVERY):
        line = "is on the way — it has shipped and should arrive soon"
    elif order.status is OrderStatus.CANCELLED:
        line = "was cancelled"
    else:
        line = f"was placed on {speak_date(order.placed_at)} and hasn't shipped yet"
    return await make_wrap(
        deps,
        flow_manager,
        f"Tell the user: order {order.id} with {titles} {line}. Ask if they need anything else.",
        name="order_status_report",
    )


async def make_refund_status_report(deps: SessionDeps, flow_manager) -> dict:
    assert deps.customer is not None
    views = await deps.store.refunds_for_customer(deps.customer.id)
    if not views:
        return await make_nothing_here(
            deps, flow_manager, "Tell the user: there are no refunds on this account."
        )
    lines = []
    for v in views:
        r = v.refund
        titles = ", ".join(v.order_item_titles)
        lines.append(
            f"Refund {r.id} for {titles}: {speak_money(r.amount)} to {r.method.value}, "
            f"initiated {speak_date(r.initiated_at)}, expected by {speak_date(r.expected_by)}, "
            f"status {r.status.value}."
        )
    return await make_wrap(
        deps,
        flow_manager,
        "Tell the user about their refunds: "
        + " ".join(lines)
        + " Ask if they need anything else.",
        name="refund_status_report",
    )


async def make_select_item(deps: SessionDeps, flow_manager, order_id: OrderId) -> dict:
    items = await deps.store.items_for_order(order_id)
    listing = "\n".join(
        f"{i + 1}. {d.product.title} ({speak_money(d.item.price)})" for i, d in enumerate(items)
    )
    valid = {str(int(d.item.id)): d.product.title for d in items}

    async def handler(args, flow_manager):
        item_id = args["order_item_id"]
        if item_id not in valid:
            return {"error": "unknown item"}, None
        d = _deps(flow_manager)
        d.wip["order_item_id"] = int(item_id)
        d.wip["item_title"] = valid[item_id]
        return None, await make_select_reason(d, flow_manager)

    select = FlowsFunctionSchema(
        name="select_item",
        description="Record which item from the order the user means.",
        properties={"order_item_id": {"type": "string", "enum": list(valid)}},
        required=["order_item_id"],
        handler=handler,
    )
    return {
        "name": "select_item",
        "task_messages": task_messages(
            deps,
            f"Ask which item they mean:\n{listing}\nCall select_item with the matching id.",
        ),
        "functions": [select, _routing("go_back", "The user wants the main menu.", make_triage)],
    }


async def make_select_reason(deps: SessionDeps, flow_manager) -> dict:
    async def handler(args, flow_manager):
        reason = args["reason"]
        d = _deps(flow_manager)
        d.wip["reason"] = reason
        return None, await make_select_resolution(d, flow_manager)

    select = FlowsFunctionSchema(
        name="select_reason",
        description="Record why the user wants to return or exchange the item.",
        properties={"reason": {"type": "string", "enum": [r.value for r in ReturnReason]}},
        required=["reason"],
        handler=handler,
    )
    return {
        "name": "select_reason",
        "task_messages": task_messages(
            deps,
            f"Ask what the problem is with the {deps.wip.get('item_title', 'item')}: damaged, "
            "defective, wrong item, missing parts, not needed any more, or a size issue. "
            "Call select_reason with the closest match.",
        ),
        "functions": [select, _routing("go_back", "The user wants the main menu.", make_triage)],
    }


async def make_select_resolution(deps: SessionDeps, flow_manager) -> dict:
    from store.domain import OrderItemId

    item_id = OrderItemId(deps.wip["order_item_id"])
    order = await deps.store.order_with_items(OrderId(deps.wip["order_id"]))
    details = await deps.store.items_for_order(order.id)
    detail = next(d for d in details if d.item.id == item_id)
    reason = ReturnReason(deps.wip["reason"])
    priors = await deps.store.return_requests_for_item(item_id)
    in_stock = await deps.store.variant_in_stock(detail.item.variant_id)

    try:
        offered = valid_resolutions(
            policy=detail.product.return_policy_type,
            reason=reason,
            delivered_at=order.delivered_at,
            now=deps.clock.now(),
            same_variant_in_stock=in_stock,
            prior_requests=priors,
        )
    except PolicyError as e:
        return await make_nothing_here(
            deps, flow_manager, f"Tell the user this isn't possible: {e.message}"
        )

    deps.wip["offered"] = sorted(r.value for r in offered)

    async def handler(args, flow_manager):
        resolution = args["resolution"]
        d = _deps(flow_manager)
        if resolution not in d.wip["offered"]:
            return {"error": "not offered"}, None
        d.wip["resolution"] = resolution
        return None, await _after_resolution(d, flow_manager)

    if offered == {Resolution.REFUND}:
        if reason in (ReturnReason.NOT_NEEDED, ReturnReason.SIZE_ISSUE):
            why = "For this item a refund is available."
        else:
            from store.policy import explain_missing_replacement

            missing = explain_missing_replacement(
                same_variant_in_stock=in_stock, prior_requests=priors
            )
            why = f"A replacement isn't possible: {missing.message} A refund is available instead."
        ask = f"{why} Ask if they'd like the refund, then call select_resolution."
    else:
        ask = (
            "Ask whether they want a refund or a free replacement, then call "
            "select_resolution with their choice."
        )

    select = FlowsFunctionSchema(
        name="select_resolution",
        description="Record whether the user wants a refund or a replacement.",
        properties={"resolution": {"type": "string", "enum": deps.wip["offered"]}},
        required=["resolution"],
        handler=handler,
    )
    return {
        "name": "select_resolution",
        "task_messages": task_messages(deps, ask),
        "functions": [select, _routing("go_back", "The user wants the main menu.", make_triage)],
    }


async def _after_resolution(deps: SessionDeps, flow_manager) -> dict:
    order = await deps.store.order_with_items(OrderId(deps.wip["order_id"]))
    if (
        deps.wip["resolution"] == Resolution.REFUND.value
        and order.payment_method is PaymentMethod.POD
    ):
        return await make_select_refund_destination(deps, flow_manager)
    return await _build_case_confirm(deps, flow_manager, destination=None)


async def make_select_refund_destination(deps: SessionDeps, flow_manager) -> dict:
    async def handler(args, flow_manager):
        d = _deps(flow_manager)
        destination = {"bank_transfer": RefundMethod.NEFT, "cheque": RefundMethod.CHEQUE}[
            args["destination"]
        ]
        return None, await _build_case_confirm(d, flow_manager, destination=destination)

    select = FlowsFunctionSchema(
        name="select_refund_destination",
        description="Record where the Pay on Delivery refund should go.",
        properties={"destination": {"type": "string", "enum": ["bank_transfer", "cheque"]}},
        required=["destination"],
        handler=handler,
    )
    return {
        "name": "select_refund_destination",
        "task_messages": task_messages(
            deps,
            "This order was Pay on Delivery, so ask where the refund should go: a bank "
            "transfer (about 5 working days) or a cheque (about 10 working days). Then "
            "call select_refund_destination.",
        ),
        "functions": [select, _routing("go_back", "The user wants the main menu.", make_triage)],
    }


async def _build_cancel_confirm(deps: SessionDeps, flow_manager, order_id: OrderId) -> dict:
    order = await deps.store.order_with_items(order_id)
    try:
        from store.policy import check_cancellable

        check_cancellable(order)
    except PolicyError as e:
        return await make_nothing_here(
            deps, flow_manager, f"Tell the user this isn't possible: {e.message}"
        )

    details = await deps.store.items_for_order(order_id)
    titles = ", ".join(d.product.title for d in details)
    band = deps.tracker.band
    if order.payment_method is PaymentMethod.POD:
        refund_line = ""
    else:
        method, expected = refund_expectation(order.payment_method, deps.clock.now())
        refund_line = refund_line_for(
            band, amount=order.total, method=method.value, expected=expected
        )

    pending = PendingMutation(
        op="cancel_order",
        args={"order_id": str(order_id)},
        readback=cancel_readback(
            band, order_id=str(order_id), titles=titles, refund_line=refund_line
        ),
    )
    return await make_confirm(deps, flow_manager, pending)


async def _build_case_confirm(
    deps: SessionDeps, flow_manager, *, destination: RefundMethod | None
) -> dict:
    order = await deps.store.order_with_items(OrderId(deps.wip["order_id"]))
    details = await deps.store.items_for_order(order.id)
    detail = next(d for d in details if int(d.item.id) == deps.wip["order_item_id"])
    reason = ReturnReason(deps.wip["reason"])
    band = deps.tracker.band
    title = detail.product.title
    reason_spoken = _REASON_SPOKEN[reason]

    if deps.wip["resolution"] == Resolution.REPLACEMENT.value:
        pending = PendingMutation(
            op="create_replacement",
            args={"order_item_id": deps.wip["order_item_id"], "reason": reason.value},
            readback=replacement_readback(band, title=title, reason=reason_spoken),
        )
    else:
        amount = detail.item.price * detail.item.quantity
        method, expected = refund_expectation(order.payment_method, deps.clock.now(), destination)
        refund_line = refund_line_for(band, amount=amount, method=method.value, expected=expected)
        pending = PendingMutation(
            op="create_return",
            args={
                "order_item_id": deps.wip["order_item_id"],
                "reason": reason.value,
                "refund_destination": destination.value if destination else None,
            },
            readback=return_readback(
                band, title=title, reason=reason_spoken, refund_line=refund_line
            ),
        )
    return await make_confirm(deps, flow_manager, pending)


async def make_nothing_here(deps: SessionDeps, flow_manager, message: str) -> dict:
    return {
        "name": "nothing_here",
        "task_messages": task_messages(deps, f"{message} Ask if they need anything else."),
        "functions": _wrap_tools(),
    }


async def make_wrap(deps: SessionDeps, flow_manager, text: str, *, name: str = "wrap") -> dict:
    return {
        "name": name,
        "task_messages": task_messages(
            deps,
            f"{text}\nIf the user brings up another order or task, call the matching "
            "function right away — never ask permission, never mention tools.",
        ),
        "functions": _wrap_tools(),
    }


async def make_end(deps: SessionDeps, flow_manager) -> dict:
    return {
        "name": "end",
        "task_messages": task_messages(deps, "Say a short, warm goodbye."),
        "functions": [],
        "post_actions": [{"type": "end_conversation"}],
    }
