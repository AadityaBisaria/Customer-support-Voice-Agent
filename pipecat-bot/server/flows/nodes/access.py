"""Unauthenticated greeting and deterministic account-verification nodes."""

from loguru import logger

from dialogue.phone import PhoneSlot
from store.domain import PhoneNumber

from ..gate_processor import GATE_STATE_KEY, SlotGate
from .menu import menu_tools
from .shared import routing, task_messages

_PHONE = PhoneSlot(name="phone", prompt="Apna registered mobile number boliye")


def _verification_prompt(flow_manager) -> str:
    """Keep legacy node wiring aligned with the active stack-based runtime."""
    stack = getattr(flow_manager, "stack", None)
    suspended = stack.frames[-2] if stack and len(stack.frames) >= 2 else None
    flow_id = getattr(suspended, "flow_id", None)
    prompts = {
        "order_status": "आपके orders retrieve करने के लिए मुझे आपका registered mobile number चाहिए।",
        "cancel_order": "आपका cancellation request check करने के लिए मुझे आपका registered mobile number चाहिए।",
        "return_order": "Return request शुरू करने के लिए अपना registered mobile number बताइए।",
        "exchange_item": "Exchange options check करने के लिए अपना registered mobile number बताइए।",
        "reschedule_delivery": "Delivery reschedule करने के लिए अपना registered mobile number बताइए।",
        "missing_delivery": "Delivery investigation open करने के लिए मुझे आपका registered mobile number चाहिए।",
    }
    return prompts.get(flow_id, "अपना registered mobile number बताइए।")


async def make_greet_unauth(deps, flow_manager) -> dict:
    return {
        "name": "greet_unauth",
        "task_messages": task_messages(
            deps,
            "Greet the caller briefly: you are an assistant for Aryan Retail returns, refunds, "
            "replacements, and delivery questions. Answer general policy questions directly. "
            "The moment they mention THEIR order or account in any way — my order, mera order, a return, "
            "a refund, a cancellation — call start_order_help RIGHT AWAY. "
            "Use the most specific start tool for the request. Never ask permission to use it, "
            "never announce it, and never mention tools: function calls are invisible to the caller.",
        ),
        "functions": [
            routing(
                "start_order_help",
                "Call immediately (without asking) when the user mentions their order or account: "
                "status, return, exchange, replacement, refund, delivery reschedule, dispute, or cancellation.",
                _order_help_entry,
            ),
            routing(
                "start_order_status",
                "The caller wants their order list, order status, tracking, or delivery ETA.",
                lambda d, fm: _order_help_entry(d, fm, "status"),
            ),
            routing(
                "start_return_or_exchange",
                "The caller wants a return, replacement, exchange, or a different size or color.",
                lambda d, fm: _order_help_entry(d, fm, "return"),
            ),
            routing(
                "start_refund_status",
                "The caller asks where an existing refund is.",
                lambda d, fm: _order_help_entry(d, fm, "refund"),
            ),
            routing(
                "start_cancel_order",
                "The caller wants to cancel an order that has not shipped.",
                lambda d, fm: _order_help_entry(d, fm, "cancel"),
            ),
            routing(
                "start_reschedule_delivery",
                "The caller wants to postpone or reschedule an incoming shipment.",
                lambda d, fm: _order_help_entry(d, fm, "reschedule"),
            ),
            routing(
                "start_dispute_delivery",
                "The caller says a delivered package was not received, was empty, or was left incorrectly.",
                lambda d, fm: _order_help_entry(d, fm, "dispute"),
            ),
        ],
    }


async def _order_help_entry(deps, flow_manager, intent: str | None = None) -> dict:
    if intent is not None:
        deps.wip["pending_intent"] = intent
    if deps.customer is not None:
        return await _continue_after_auth(deps, flow_manager)
    return await make_verify_phone(deps, flow_manager)


async def _continue_after_auth(deps, flow_manager) -> dict:
    """Resume a specific request after deterministic account verification."""
    intent = deps.wip.pop("pending_intent", None)
    if intent in {"status", "return", "cancel", "reschedule", "dispute"}:
        from .journeys import make_select_order

        return await make_select_order(deps, flow_manager, intent)
    if intent == "refund":
        from .journeys import make_refund_status_report

        return await make_refund_status_report(deps, flow_manager)
    return await make_triage(deps, flow_manager, greet=True)


async def make_verify_phone(deps, flow_manager, *, note: str | None = None) -> dict:
    async def on_fit(digits: str) -> None:
        customer = await deps.store.customer_by_phone(PhoneNumber.parse(digits))
        if customer is not None:
            deps.customer = customer
            deps.verify_attempts = 0
            caller_name = getattr(customer, "name", f"{customer.first_name} {customer.last_name}".strip())
            logger.info("verified caller {} via phone", caller_name)
            await flow_manager.set_node_from_config(await _continue_after_auth(deps, flow_manager))
            return

        deps.verify_attempts += 1
        if deps.verify_attempts >= 2:
            await flow_manager.set_node_from_config(await make_kb_only(deps, flow_manager))
            return

        await flow_manager.set_node_from_config(
            await make_verify_phone(deps, flow_manager, note="That number didn't match an account.")
        )

    async def on_exhausted() -> None:
        # A later successful verification always restarts the counter at zero;
        # reset here too so this fallback cannot poison a future auth attempt.
        deps.verify_attempts = 0
        await flow_manager.set_node_from_config(await make_kb_only(deps, flow_manager))

    flow_manager.state[GATE_STATE_KEY] = SlotGate(
        slot=_PHONE, on_fit=on_fit, on_exhausted=on_exhausted, max_attempts=3
    )
    prefix = f"{note} " if note else ""
    prompt = _verification_prompt(flow_manager)
    return {
        "name": "verify_phone",
        "task_messages": task_messages(
            deps,
            f"{prefix}{prompt} "
            "If what they said wasn't a number, ask again.",
        ),
        "functions": [],
    }


async def make_kb_only(deps, flow_manager) -> dict:
    return {
        "name": "kb_only",
        "task_messages": task_messages(
            deps,
            "The caller could not be verified. Apologize briefly; say you can still answer general questions "
            "about returns, refunds, replacements, and delivery policies, but account-specific help needs "
            "a call from the registered number. Answer their policy questions.",
        ),
        "functions": [],
    }


async def make_triage(deps, flow_manager, *, greet: bool = False) -> dict:
    deps.wip.clear()
    assert deps.customer is not None
    caller_name = getattr(deps.customer, "name", deps.customer.first_name)
    greeting = f"Greet the caller by name — they are {caller_name} — and ask how you can help. " if greet else ""
    return {
        "name": "triage",
        "task_messages": task_messages(
            deps,
            f"{greeting}You can check order status, start a return or size exchange, reschedule an incoming delivery, "
            "check refund status, dispute a missing package, or cancel an unshipped order. "
            "Call the matching tool when the user asks. Answer general policy questions directly.",
        ),
        "functions": menu_tools(),
    }
