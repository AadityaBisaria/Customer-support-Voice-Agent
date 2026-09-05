"""Graph invariants and transitions — no model, no pipeline.

The reference-architecture invariants live here: mutations are never tools,
routing carries no data, selection enums are live IDs, gate nodes are
toolless, and RESET is never used.
"""

import subprocess
import sys

import pytest

from flows import GATE_STATE_KEY, is_rag_active
from flows.nodes import (
    _after_order_selected,
    _after_resolution,
    make_greet_unauth,
    make_refund_status_report,
    make_select_order,
    make_select_resolution,
    make_triage,
    make_verify_phone,
)
from store.domain import OrderId, PhoneNumber

from .conftest import FakeFlowManager, tool_names

MUTATION_NAMES = {"cancel_order", "create_return", "create_replacement"}


async def all_reachable_nodes(deps, fm) -> list[dict]:
    """Build one instance of every factory-producible node for sweeps."""
    nodes = [
        await make_greet_unauth(deps, fm),
        await make_verify_phone(deps, fm),
        await make_triage(deps, fm, greet=True),
        await make_select_order(deps, fm, "status"),
        await make_select_order(deps, fm, "return"),
        await make_select_order(deps, fm, "cancel"),
        await make_refund_status_report(deps, fm),
    ]
    # Walk a return down to the confirm node (2-item order -> select_item).
    nodes.append(await _after_order_selected(deps, fm, "return", OrderId("AMZ-1001")))
    deps.wip.update(
        {"order_id": "AMZ-1001", "order_item_id": 1, "item_title": "kurta", "reason": "damaged"}
    )
    nodes.append(await make_select_resolution(deps, fm))
    deps.wip["resolution"] = "refund"
    nodes.append(await _after_resolution(deps, fm))  # confirm_mutation
    # And a cancel confirm.
    nodes.append(await _after_order_selected(deps, fm, "cancel", OrderId("AMZ-1004")))
    return nodes


class TestInvariants:
    async def test_mutations_are_never_tools(self, deps, fm, priya):
        for node in await all_reachable_nodes(deps, fm):
            assert not (tool_names(node) & MUTATION_NAMES), node["name"]

    async def test_routing_tools_carry_no_data(self, deps, fm, priya):
        for node in await all_reachable_nodes(deps, fm):
            for schema in node.get("functions", []):
                if schema.name.startswith(("start_", "go_back", "back_to_menu", "end_call")):
                    assert schema.properties == {}, f"{node['name']}.{schema.name}"
                    assert schema.required == [], f"{node['name']}.{schema.name}"

    async def test_no_node_sets_context_strategy(self, deps, fm, priya):
        for node in await all_reachable_nodes(deps, fm):
            assert "context_strategy" not in node, node["name"]

    async def test_menus_stay_small(self, deps, fm, priya):
        # Legacy routing menus contain six domain routes plus a fallback/end.
        # The command runtime exposes a single validated command schema instead.
        for node in await all_reachable_nodes(deps, fm):
            assert len(node.get("functions", [])) <= 7, node["name"]

    async def test_gate_nodes_are_toolless(self, deps, fm, priya):
        verify = await make_verify_phone(deps, fm)
        assert verify["functions"] == []
        deps.wip.update(
            {
                "order_id": "AMZ-1001",
                "order_item_id": 1,
                "item_title": "kurta",
                "reason": "damaged",
                "resolution": "refund",
            }
        )
        confirm = await _after_resolution(deps, fm)
        assert confirm["name"] == "confirm_mutation"
        assert confirm["functions"] == []
        assert GATE_STATE_KEY in fm.state

    def test_flows_package_never_imports_sqlite(self):
        """The layering rule: conversation code depends on ports, not sqlite."""
        code = "import flows, sys; raise SystemExit(1 if 'store.sqlite' in sys.modules else 0)"
        proc = subprocess.run(
            [sys.executable, "-c", code], cwd=str(__import__("pathlib").Path(__file__).parents[2])
        )
        assert proc.returncode == 0


class TestSelectOrder:
    async def test_enum_is_live_order_ids(self, deps, fm, priya):
        node = await make_select_order(deps, fm, "status")
        schema = next(s for s in node["functions"] if s.name == "select_order")
        assert set(schema.properties["order_id"]["enum"]) == {
            "AMZ-1001",
            "AMZ-1002",
            "AMZ-1003",
            "AMZ-1004",
        }

    async def test_purpose_filters(self, deps, fm, priya):
        cancel = await make_select_order(deps, fm, "cancel")
        schema = next(s for s in cancel["functions"] if s.name == "select_order")
        assert schema.properties["order_id"]["enum"] == ["AMZ-1004"]  # only placed

    async def test_no_orders_goes_to_nothing_here(self, deps, fm):
        deps.customer = await deps.store.customer_by_phone(PhoneNumber.parse("9111111111"))  # Vikram
        node = await make_select_order(deps, fm, "status")
        assert node["name"] == "nothing_here"

    async def test_handler_rejects_out_of_enum_id(self, deps, fm, priya):
        node = await make_select_order(deps, fm, "status")
        schema = next(s for s in node["functions"] if s.name == "select_order")
        result, next_node = await schema.handler({"order_id": "AMZ-9999"}, fm)
        assert next_node is None and "error" in result


class TestReturnPath:
    async def test_single_item_order_skips_item_select(self, deps, fm, priya):
        node = await _after_order_selected(deps, fm, "return", OrderId("AMZ-1002"))
        assert node["name"] == "select_reason"
        assert deps.wip["order_item_id"] == 3

    async def test_two_item_order_asks_which_item(self, deps, fm, priya):
        node = await _after_order_selected(deps, fm, "return", OrderId("AMZ-1001"))
        assert node["name"] == "select_item"

    async def test_resolution_enum_is_policy_restricted(self, deps, fm, priya):
        # AMZ-3003 lamp: damage-class but variant out of stock -> refund only.
        deps.customer = await deps.store.customer_by_phone(PhoneNumber.parse("9000000001"))
        deps.wip.update(
            {"order_id": "AMZ-3003", "order_item_id": 11, "item_title": "lamp", "reason": "damaged"}
        )
        node = await make_select_resolution(deps, fm)
        schema = next(s for s in node["functions"] if s.name == "select_resolution")
        assert schema.properties["resolution"]["enum"] == ["refund"]
        assert 'refund' in node['speech']
        assert 'replacement' not in node['speech']

    async def test_out_of_window_goes_to_nothing_here(self, deps, fm):
        deps.customer = await deps.store.customer_by_phone(PhoneNumber.parse("9000000001"))
        deps.wip.update(
            {
                "order_id": "AMZ-3001",
                "order_item_id": 9,
                "item_title": "sandals",
                "reason": "not_needed",
            }
        )
        node = await make_select_resolution(deps, fm)
        assert node["name"] == "nothing_here"
        assert "closed" in node["task_messages"][0]["content"]

    async def test_cod_refund_uses_verified_saved_destination(self, deps, fm):
        deps.customer = await deps.store.customer_by_phone(PhoneNumber.parse("9123456789"))
        deps.wip.update(
            {
                "order_id": "AMZ-2001",
                "order_item_id": 6,
                "item_title": "mixer",
                "reason": "not_needed",
                "resolution": "refund",
            }
        )
        node = await _after_resolution(deps, fm)
        assert node["name"] == "confirm_mutation"

    async def test_prepaid_refund_goes_straight_to_confirm(self, deps, fm, priya):
        deps.wip.update(
            {
                "order_id": "AMZ-1001",
                "order_item_id": 1,
                "item_title": "kurta",
                "reason": "damaged",
                "resolution": "refund",
            }
        )
        node = await _after_resolution(deps, fm)
        assert node["name"] == "confirm_mutation"


class TestCancelPath:
    async def test_shipped_order_is_refused_in_code(self, deps, fm):
        deps.customer = await deps.store.customer_by_phone(PhoneNumber.parse("9123456789"))
        node = await _after_order_selected(deps, fm, "cancel", OrderId("AMZ-2002"))
        assert node["name"] == "nothing_here"
        assert "shipped" in node["task_messages"][0]["content"]

    async def test_placed_order_reaches_confirm_with_readback(self, deps, fm, priya):
        node = await _after_order_selected(deps, fm, "cancel", OrderId("AMZ-1004"))
        assert node["name"] == "confirm_mutation"
        content = node["task_messages"][0]["content"]
        assert "AMZ-1004" in content and "phone case" in content


class TestVerifyPhone:
    async def test_right_number_reaches_triage(self, deps, fm):
        await make_verify_phone(deps, fm)
        gate = fm.state[GATE_STATE_KEY]
        await gate.on_fit("9876543210")
        assert fm.current_node == "triage"
        assert deps.customer is not None and deps.customer.first_name == "Priya"

    async def test_two_wrong_numbers_reach_kb_only(self, deps, fm):
        await make_verify_phone(deps, fm)
        await fm.state[GATE_STATE_KEY].on_fit("9999999991")
        assert fm.current_node == "verify_phone"
        await fm.state[GATE_STATE_KEY].on_fit("9999999992")
        assert fm.current_node == "kb_only"


class TestRefundStatus:
    async def test_report_renders_seeded_refund(self, deps, fm):
        deps.customer = await deps.store.customer_by_phone(PhoneNumber.parse("9000000001"))
        node = await make_refund_status_report(deps, fm)
        content = node["task_messages"][0]["content"]
        assert "REF-2001" in content and "1799 rupees" in content

    async def test_no_refunds_is_nothing_here(self, deps, fm, priya):
        node = await make_refund_status_report(deps, fm)
        assert node["name"] == "nothing_here"


@pytest.mark.parametrize(
    "node_name, active",
    [
        ("greet_unauth", True),
        ("kb_only", True),
        ("triage", True),
        ("select_order_status", False),
        ("confirm_mutation", False),
        (None, True),
    ],
)
def test_rag_active_nodes(node_name, active):
    class FM:
        current_node = node_name

    assert is_rag_active(FM() if node_name is not None else None) is active
