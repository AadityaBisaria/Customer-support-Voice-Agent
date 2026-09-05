"""Small, node-local menus for top-level support actions."""

from .shared import routing


def menu_tools():
    # Local imports keep node factories independently importable and avoid cycles.
    from .journeys import make_refund_status_report, make_select_order

    return [
        routing(
            "start_order_status",
            "The user asks where an order is, tracking info, or estimated arrival. "
            "NOT for changing the delivery date or filing a dispute.",
            lambda d, fm: make_select_order(d, fm, "status"),
        ),
        routing(
            "start_return_or_exchange",
            "The user wants to return an item for a refund, or exchange size/color for apparel. "
            "NOT for cancelling an unshipped order, and NOT for packages that never arrived.",
            lambda d, fm: make_select_order(d, fm, "return"),
        ),
        routing(
            "start_reschedule_delivery",
            "The user wants to change, postpone, or pick a morning/afternoon slot for an incoming shipment. "
            "NOT for checking tracking status without changes.",
            lambda d, fm: make_select_order(d, fm, "reschedule"),
        ),
        routing(
            "start_dispute_delivery",
            "The user reports a delivery issue: tracking shows delivered but not received, an empty box, or wrong address. "
            "NOT for regular returns or items still in transit.",
            lambda d, fm: make_select_order(d, fm, "dispute"),
        ),
        routing(
            "start_refund_status",
            "The user asks about the status of money or refund from a previous return or cancellation. "
            "NOT for requesting a new return.",
            make_refund_status_report,
        ),
        routing(
            "start_cancel_order",
            "The user wants to cancel an order that has not yet shipped. "
            "NOT for returning an order that has already been delivered.",
            lambda d, fm: make_select_order(d, fm, "cancel"),
        ),
    ]


def wrap_tools():
    from .terminal import make_end

    return [
        *menu_tools(),
        routing(
            "end_call",
            "The user explicitly indicates they are done, says thanks, goodbye, or hangs up.",
            make_end,
        ),
    ]