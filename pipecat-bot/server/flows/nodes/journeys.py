"""Order selection, status, cancellation, return, exchange, reschedule, and dispute journeys."""

from datetime import timedelta
from typing import Literal

from pipecat.flows import FlowsFunctionSchema

from store.domain import (
    DeliverySlotPreference,
    DisputeType,
    OrderId,
    OrderItemId,
    OrderStatus,
    PaymentMethod,
    RefundDestination,
    RefundMethod,
    Resolution,
    ReturnReason,
    VariantId,
)
from store.policy import PolicyError, refund_expectation, valid_resolutions

from ..confirm import make_confirm
from ..pending import (
    PendingMutation,
    cancel_readback,
    dispute_readback,
    exchange_readback,
    refund_line_for,
    replacement_readback,
    reschedule_readback,
    return_readback,
    speak_date,
    speak_money,
)
from .access import make_triage
from .shared import deps_for, routing, task_messages
from .terminal import make_nothing_here, make_wrap

Purpose = Literal["status", "return", "cancel", "reschedule", "dispute"]

_PURPOSE_FILTER: dict[Purpose, set[OrderStatus] | None] = {
    "status": None,
    "return": {OrderStatus.DELIVERED},
    "cancel": {OrderStatus.PLACED},
    "reschedule": {OrderStatus.SHIPPED, OrderStatus.OUT_FOR_DELIVERY},
    "dispute": {OrderStatus.DELIVERED},
}

_PURPOSE_EMPTY = {
    "status": "Tell the user: there are no orders on this account.",
    "return": "Tell the user: there are no delivered orders that could be returned.",
    "cancel": "Tell the user: there are no orders that can still be cancelled — cancellation is only possible before an order ships.",
    "reschedule": "Tell the user: there are no shipments currently on the way to reschedule.",
    "dispute": "Tell the user: there are no delivered packages on file to dispute.",
}

_REASON_SPOKEN = {
    ReturnReason.DAMAGED: "it arrived damaged",
    ReturnReason.DEFECTIVE: "it is defective",
    ReturnReason.WRONG_ITEM: "a wrong item arrived",
    ReturnReason.MISSING_PARTS: "parts are missing",
    ReturnReason.NOT_NEEDED: "no longer needed",
    ReturnReason.SIZE_ISSUE: "size or fit issue",
}


def _order_line(index: int, order) -> str:
    titles = ", ".join(order.item_titles)
    when = speak_date(order.delivered_at) if order.delivered_at else speak_date(order.placed_at)
    return f"{index}. {order.id}: {titles} ({'delivered' if order.delivered_at else 'placed'} {when}, {speak_money(order.total)})"


async def make_select_order(deps, flow_manager, purpose: Purpose) -> dict:
    assert deps.customer is not None
    orders = await deps.store.orders_for_customer(deps.customer.id, statuses=_PURPOSE_FILTER[purpose])
    if not orders:
        return await make_nothing_here(deps, flow_manager, _PURPOSE_EMPTY[purpose])
    deps.wip["purpose"] = purpose
    valid_ids = [str(o.id) for o in orders]

    async def handler(args, flow_manager):
        order_id = args["order_id"]
        if order_id not in valid_ids:
            return {"error": "unknown order"}, None
        d = deps_for(flow_manager)
        d.wip["order_id"] = order_id
        return None, await _after_order_selected(d, flow_manager, purpose, OrderId(order_id))

    select = FlowsFunctionSchema(
        name="select_order",
        description="Record which of the listed orders the user means.",
        properties={"order_id": {"type": "string", "enum": valid_ids}},
        required=["order_id"],
        handler=handler,
    )
    listing = "\n".join(_order_line(i + 1, o) for i, o in enumerate(orders))
    return {
        "name": f"select_order_{purpose}",
        "task_messages": task_messages(
            deps,
            f"Ask which order the user means, offering these:\n{listing}\n"
            "Call select_order with the matching order id. If they change their mind, call go_back.",
        ),
        "functions": [select, routing("go_back", "The user wants the main menu.", make_triage)],
    }


async def _after_order_selected(deps, flow_manager, purpose: Purpose, order_id: OrderId) -> dict:
    if purpose == "status":
        return await make_order_status_report(deps, flow_manager, order_id)
    if purpose == "cancel":
        return await _build_cancel_confirm(deps, flow_manager, order_id)
    if purpose == "reschedule":
        return await make_select_reschedule_date(deps, flow_manager, order_id)
    if purpose == "dispute":
        return await make_select_dispute_reason(deps, flow_manager, order_id)

    items = await deps.store.items_for_order(order_id)
    if len(items) == 1:
        deps.wip["order_item_id"] = int(items[0].item.id)
        deps.wip["item_title"] = items[0].product.title
        return await make_select_reason(deps, flow_manager)
    return await make_select_item(deps, flow_manager, order_id)


async def make_order_status_report(deps, flow_manager, order_id: OrderId) -> dict:
    order, items = await deps.store.order_with_items(order_id), await deps.store.items_for_order(order_id)
    delivery = await deps.store.delivery_for_order(order_id)
    titles = ", ".join(d.product.title for d in items)

    if order.status is OrderStatus.DELIVERED:
        line = f"was delivered on {speak_date(order.delivered_at)}"
    elif order.status in (OrderStatus.SHIPPED, OrderStatus.OUT_FOR_DELIVERY):
        eta = (
            f", arriving by {speak_date(delivery.estimated_delivery_date)}"
            if delivery and delivery.estimated_delivery_date
            else ""
        )
        line = f"is on the way via {delivery.courier_partner if delivery else 'courier'}{eta}"
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


async def make_refund_status_report(deps, flow_manager) -> dict:
    assert deps.customer is not None
    views = await deps.store.refunds_for_customer(deps.customer.id)
    if not views:
        return await make_nothing_here(deps, flow_manager, "Tell the user: there are no refunds on this account.")
    lines = [
        f"Refund {v.refund.id} for {', '.join(v.order_item_titles)}: {speak_money(v.refund.amount)} to {v.refund.method.value}, "
        f"initiated {speak_date(v.refund.initiated_at)}, expected by {speak_date(v.refund.expected_by)}, status {v.refund.status.value}."
        for v in views
    ]
    return await make_wrap(
        deps,
        flow_manager,
        "Tell the user about their refunds: " + " ".join(lines) + " Ask if they need anything else.",
        name="refund_status_report",
    )


async def make_select_item(deps, flow_manager, order_id: OrderId) -> dict:
    items = await deps.store.items_for_order(order_id)
    valid = {str(int(d.item.id)): d.product.title for d in items}

    async def handler(args, flow_manager):
        item_id = args["order_item_id"]
        if item_id not in valid:
            return {"error": "unknown item"}, None
        d = deps_for(flow_manager)
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
    listing = "\n".join(f"{i + 1}. {d.product.title} ({speak_money(d.item.price)})" for i, d in enumerate(items))
    return {
        "name": "select_item",
        "task_messages": task_messages(
            deps, f"Ask which item they mean:\n{listing}\nCall select_item with the matching id."
        ),
        "functions": [select, routing("go_back", "The user wants the main menu.", make_triage)],
    }


async def make_select_reason(deps, flow_manager) -> dict:
    async def handler(args, flow_manager):
        d = deps_for(flow_manager)
        d.wip["reason"] = args["reason"]
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
            f"Ask what the problem is with the {deps.wip.get('item_title', 'item')}: damaged, defective, wrong item, "
            "missing parts, not needed any more, or a size issue. Call select_reason with the closest match.",
        ),
        "functions": [select, routing("go_back", "The user wants the main menu.", make_triage)],
    }


async def make_select_resolution(deps, flow_manager) -> dict:
    item_id = OrderItemId(deps.wip["order_item_id"])
    order = await deps.store.order_with_items(OrderId(deps.wip["order_id"]))
    detail = next(d for d in await deps.store.items_for_order(order.id) if d.item.id == item_id)
    reason = ReturnReason(deps.wip["reason"])
    priors = await deps.store.return_requests_for_item(item_id)

    variants = await deps.store.variants_for_product(detail.product.product_id)
    has_exchangeable_variants = any(
        variant.variant_id != detail.item.variant_id and variant.stock_count > 0
        for variant in variants
    )

    try:
        offered = valid_resolutions(
            policy=detail.product.return_policy_type,
            category=detail.product.category,
            reason=reason,
            delivered_at=order.delivered_at,
            now=deps.clock.now(),
            same_variant_in_stock=await deps.store.variant_in_stock(detail.item.variant_id),
            has_exchangeable_variants=has_exchangeable_variants,
            prior_requests=priors,
        )
    except PolicyError as e:
        return await make_nothing_here(deps, flow_manager, f"Tell the user this isn't possible: {e.message}")

    deps.wip["offered"] = sorted(r.value for r in offered)
    deps.wip["product_id"] = detail.product.product_id
    deps.wip["variant_id"] = str(detail.item.variant_id)

    async def handler(args, flow_manager):
        d = deps_for(flow_manager)
        resolution = args["resolution"]
        if resolution not in d.wip["offered"]:
            return {"error": "not offered"}, None
        d.wip["resolution"] = resolution
        return None, await _after_resolution(d, flow_manager)

    select = FlowsFunctionSchema(
        name="select_resolution",
        description="Record whether the user wants a refund, replacement, or exchange.",
        properties={"resolution": {"type": "string", "enum": deps.wip["offered"]}},
        required=["resolution"],
        handler=handler,
    )
    return {
        "name": "select_resolution",
        "task_messages": task_messages(
            deps, f"Offer these available options: {', '.join(deps.wip['offered'])}. Call select_resolution with their choice."
        ),
        "functions": [select, routing("go_back", "The user wants the main menu.", make_triage)],
    }


async def _after_resolution(deps, flow_manager) -> dict:
    resolution = Resolution(deps.wip["resolution"])
    if resolution is Resolution.EXCHANGE:
        return await make_select_exchange_variant(deps, flow_manager)

    order = await deps.store.order_with_items(OrderId(deps.wip["order_id"]))
    if resolution is Resolution.REFUND and order.payment_method is PaymentMethod.CASH_ON_DELIVERY:
        assert deps.customer is not None
        account = await deps.store.account_for_customer(deps.customer.id)
        if account and account.default_refund_destination is not None:
            return await _build_case_confirm(
                deps, flow_manager, destination=account.default_refund_destination
            )
        return await make_select_payout_destination(deps, flow_manager)

    return await _build_case_confirm(deps, flow_manager, destination=None)


async def make_select_payout_destination(deps, flow_manager) -> dict:
    return await make_nothing_here(
        deps,
        flow_manager,
        "Tell the user a Cash on Delivery refund needs a verified UPI or bank account on file. "
        "Ask them to update it through account settings before proceeding.",
    )


async def make_select_exchange_variant(deps, flow_manager) -> dict:
    variants = await deps.store.variants_for_product(deps.wip["product_id"])
    current_variant_id = deps.wip["variant_id"]
    available = [
        variant
        for variant in variants
        if str(variant.variant_id) != current_variant_id and variant.stock_count > 0
    ]
    if not available:
        return await make_nothing_here(
            deps,
            flow_manager,
            "Tell the user other sizes or colors for this item are currently out of stock. Offer a refund instead.",
        )

    labels = {
        str(variant.variant_id): " ".join(
            value for value in (variant.attributes.get("size"), variant.attributes.get("color")) if value
        ) or variant.sku
        for variant in available
    }

    async def handler(args, flow_manager):
        variant_id = args["variant_id"]
        if variant_id not in labels:
            return {"error": "invalid variant"}, None
        d = deps_for(flow_manager)
        d.wip["new_variant_id"] = int(variant_id)
        d.wip["new_variant_label"] = labels[variant_id]
        return None, await _build_exchange_confirm(d, flow_manager)

    select = FlowsFunctionSchema(
        name="select_variant",
        description="Select the requested in-stock size or color.",
        properties={"variant_id": {"type": "string", "enum": list(labels)}},
        required=["variant_id"],
        handler=handler,
    )
    choices = ", ".join(f"{label} (Option: {variant_id})" for variant_id, label in labels.items())
    return {
        "name": "select_exchange_variant",
        "task_messages": task_messages(
            deps,
            f"Ask which in-stock size or color they prefer: {choices}. Call select_variant with their choice.",
        ),
        "functions": [select, routing("go_back", "The user wants the main menu.", make_triage)],
    }


# ------------------------------------------------------------- Reschedule Shipment Journey
async def make_select_reschedule_date(deps, flow_manager, order_id: OrderId) -> dict:
    delivery = await deps.store.delivery_for_order(order_id)
    if not delivery:
        return await make_nothing_here(deps, flow_manager, "Tell the user there is no courier tracking on file for this order.")

    now = deps.clock.now()
    days_map = {
        str(i): (now + timedelta(days=i)).strftime("%A, %d %B")
        for i in range(1, 4)
    }

    async def handler(args, flow_manager):
        offset = int(args["days_ahead"])
        slot = args.get("slot")
        d = deps_for(flow_manager)
        d.wip["target_date"] = now + timedelta(days=offset)
        d.wip["slot"] = DeliverySlotPreference(slot) if slot else None
        return None, await _build_reschedule_confirm(d, flow_manager, delivery)

    select = FlowsFunctionSchema(
        name="select_reschedule_date",
        description="Select the preferred postponement date and time slot.",
        properties={
            "days_ahead": {"type": "string", "enum": list(days_map)},
            "slot": {"type": "string", "enum": ["morning", "afternoon", "evening"]},
        },
        required=["days_ahead"],
        handler=handler,
    )
    choices = ", ".join(f"{date_str} (in {day} day{'s' if day != '1' else ''})" for day, date_str in days_map.items())
    return {
        "name": "select_reschedule_date",
        "task_messages": task_messages(
            deps,
            f"Offer these dates for package delivery: {choices}. Ask if they prefer morning, afternoon, or evening. "
            "Call select_reschedule_date with their choice.",
        ),
        "functions": [select, routing("go_back", "The user wants the main menu.", make_triage)],
    }


# ------------------------------------------------------------- Missing Delivery Dispute Journey
async def make_select_dispute_reason(deps, flow_manager, order_id: OrderId) -> dict:
    delivery = await deps.store.delivery_for_order(order_id)
    reasons = {
        "item_not_received": "Marked delivered but not received",
        "empty_box": "Package arrived empty or tampered",
        "wrong_location": "Delivered to the wrong address",
    }

    async def handler(args, flow_manager):
        dtype = args["dispute_type"]
        d = deps_for(flow_manager)
        d.wip["dispute_type"] = DisputeType(dtype)
        return None, await _build_dispute_confirm(d, flow_manager, order_id, delivery)

    select = FlowsFunctionSchema(
        name="select_dispute_reason",
        description="Record the nature of the delivery issue.",
        properties={"dispute_type": {"type": "string", "enum": list(reasons)}},
        required=["dispute_type"],
        handler=handler,
    )
    return {
        "name": "select_dispute_reason",
        "task_messages": task_messages(
            deps,
            "Ask the user what happened: did the package not arrive, was the box empty, or was it left at the wrong location? "
            "Call select_dispute_reason with their response.",
        ),
        "functions": [select, routing("go_back", "The user wants the main menu.", make_triage)],
    }


# ------------------------------------------------------------- Confirm Builders
async def _build_cancel_confirm(deps, flow_manager, order_id: OrderId) -> dict:
    order = await deps.store.order_with_items(order_id)
    try:
        from store.policy import check_cancellable
        check_cancellable(order)
    except PolicyError as e:
        return await make_nothing_here(deps, flow_manager, f"Tell the user this isn't possible: {e.message}")

    titles = ", ".join(d.product.title for d in await deps.store.items_for_order(order_id))
    refund_line = (
        ""
        if order.payment_method is PaymentMethod.CASH_ON_DELIVERY
        else refund_line_for(
            deps.tracker.band,
            amount=order.total,
            method=refund_expectation(order.payment_method, deps.clock.now())[0].value,
            expected=refund_expectation(order.payment_method, deps.clock.now())[1],
        )
    )
    pending = PendingMutation(
        op="cancel_order",
        args={"order_id": str(order_id)},
        readback=cancel_readback(deps.tracker.band, order_id=str(order_id), titles=titles, refund_line=refund_line),
    )
    return await make_confirm(deps, flow_manager, pending)


async def _build_exchange_confirm(deps, flow_manager) -> dict:
    order = await deps.store.order_with_items(OrderId(deps.wip["order_id"]))
    detail = next(
        item
        for item in await deps.store.items_for_order(order.id)
        if int(item.item.id) == deps.wip["order_item_id"]
    )
    reason = ReturnReason(deps.wip["reason"])
    pending = PendingMutation(
        op="create_exchange",
        args={
            "order_item_id": deps.wip["order_item_id"],
            "new_variant_id": deps.wip["new_variant_id"],
            "reason": reason.value,
        },
        readback=exchange_readback(
            deps.tracker.band,
            title=detail.product.title,
            variant=deps.wip["new_variant_label"],
            reason=_REASON_SPOKEN[reason],
        ),
    )
    return await make_confirm(deps, flow_manager, pending)


async def _build_reschedule_confirm(deps, flow_manager, delivery) -> dict:
    target_date = deps.wip["target_date"]
    slot = deps.wip["slot"]
    pending = PendingMutation(
        op="reschedule_delivery",
        args={
            "delivery_id": int(delivery.delivery_id),
            "new_date": target_date.isoformat(),
            "slot": slot.value if slot else None,
            "instructions": None,
        },
        readback=reschedule_readback(deps.tracker.band, target_date=target_date, slot=slot.value if slot else None),
    )
    return await make_confirm(deps, flow_manager, pending)


async def _build_dispute_confirm(deps, flow_manager, order_id: OrderId, delivery) -> dict:
    dtype = deps.wip["dispute_type"]
    pending = PendingMutation(
        op="submit_dispute_ticket",
        args={
            "order_id": str(order_id),
            "delivery_id": int(delivery.delivery_id) if delivery else None,
            "customer_id": int(deps.customer.id),
            "dispute_type": dtype.value,
        },
        readback=dispute_readback(deps.tracker.band, order_id=str(order_id), dispute_type=dtype.value),
    )
    return await make_confirm(deps, flow_manager, pending)


async def _build_case_confirm(
    deps, flow_manager, *, destination: RefundDestination | None
) -> dict:
    order = await deps.store.order_with_items(OrderId(deps.wip["order_id"]))
    detail = next(d for d in await deps.store.items_for_order(order.id) if int(d.item.id) == deps.wip["order_item_id"])
    reason, band, title = ReturnReason(deps.wip["reason"]), deps.tracker.band, detail.product.title

    if deps.wip["resolution"] == Resolution.REPLACEMENT.value:
        pending = PendingMutation(
            op="create_replacement",
            args={"order_item_id": deps.wip["order_item_id"], "reason": reason.value},
            readback=replacement_readback(band, title=title, reason=_REASON_SPOKEN[reason]),
        )
    else:
        dest_kind = None
        if destination:
            dest_kind = getattr(destination, "kind", destination.get("kind") if isinstance(destination, dict) else None)
        method, expected = refund_expectation(
            order.payment_method,
            deps.clock.now(),
            RefundMethod(dest_kind) if dest_kind else None,
        )
        dest_payload = destination.model_dump() if hasattr(destination, "model_dump") else destination
        pending = PendingMutation(
            op="create_return",
            args={
                "order_item_id": deps.wip["order_item_id"],
                "reason": reason.value,
                "refund_destination": dest_payload if destination else None,
            },
            readback=return_readback(
                band,
                title=title,
                reason=_REASON_SPOKEN[reason],
                refund_line=refund_line_for(
                    band,
                    amount=detail.item.price * detail.item.quantity,
                    method=method.value,
                    expected=expected,
                ),
            ),
        )
    return await make_confirm(deps, flow_manager, pending)