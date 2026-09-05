"""The confirm gate end-to-end, model-free: readback -> haan -> mutation.

Uses the real seeded store, the real YesNoSlot via evaluate_gate, and a fake
FlowManager — the exact machinery the pipeline runs, minus audio and LLM.
"""

from flows import GATE_STATE_KEY
from flows.gate_processor import evaluate_gate
from flows.nodes import _after_order_selected, _after_resolution
from store.domain import OrderId, OrderStatus


async def arm_cancel_confirm(deps, fm):
    node = await _after_order_selected(deps, fm, "cancel", OrderId("AMZ-1004"))
    assert node["name"] == "confirm_mutation"
    return fm.state[GATE_STATE_KEY]


class TestConfirmDecision:
    async def test_haan_executes_and_lands_on_done(self, deps, fm, priya):
        gate = await arm_cancel_confirm(deps, fm)
        verdict, value = await evaluate_gate(gate, "haan kar do", deps)
        assert (verdict, value) == ("fit", True)
        await gate.on_fit(value)
        assert fm.current_node == "mutation_done"
        order = await deps.store.order_with_items(OrderId("AMZ-1004"))
        assert order.status is OrderStatus.CANCELLED
        content = fm.set_nodes[-1]["task_messages"][0]["content"]
        assert "cancelled" in content and "399 rupees" in content

    async def test_nahi_cancels_and_mutates_nothing(self, deps, fm, priya):
        gate = await arm_cancel_confirm(deps, fm)
        verdict, value = await evaluate_gate(gate, "नहीं रहने दो", deps)
        assert (verdict, value) == ("fit", False)
        await gate.on_fit(value)
        assert fm.current_node == "mutation_cancelled"
        order = await deps.store.order_with_items(OrderId("AMZ-1004"))
        assert order.status is OrderStatus.PLACED  # untouched

    async def test_backchannel_passes_through_for_a_reask(self, deps, fm, priya):
        gate = await arm_cancel_confirm(deps, fm)
        verdict, _ = await evaluate_gate(gate, "hmm achha", deps)
        assert verdict == "pass"
        assert fm.set_nodes == []  # nothing happened

    async def test_ambiguous_passes_through(self, deps, fm, priya):
        gate = await arm_cancel_confirm(deps, fm)
        verdict, _ = await evaluate_gate(gate, "haan nahi ruko", deps)
        assert verdict == "pass"

    async def test_replayed_yes_is_idempotent(self, deps, fm, priya):
        """A duplicated confirmation (barge-in replay) executes exactly once."""
        gate = await arm_cancel_confirm(deps, fm)
        await gate.on_fit(True)
        await gate.on_fit(True)  # same PendingMutation, same idempotency key
        refunds = await deps.store.refunds_for_customer(priya.customer_id)
        assert len([v for v in refunds if v.refund.order_id == "AMZ-1004"]) == 1

    async def test_raced_state_change_lands_on_failed(self, deps, fm, priya):
        gate = await arm_cancel_confirm(deps, fm)
        # The order ships between readback and the yes:
        conn = deps.store._conn  # test-only reach-around to simulate the race
        conn.execute("UPDATE orders SET status='shipped', shipped_at=placed_at WHERE id='AMZ-1004'")
        await gate.on_fit(True)
        assert fm.current_node == "mutation_failed"
        content = fm.set_nodes[-1]["task_messages"][0]["content"]
        assert "Nothing was changed" in content


class TestReturnConfirm:
    async def test_full_return_flow_creates_case(self, deps, fm, priya):
        deps.wip.update(
            {
                "order_id": "AMZ-1002",
                "order_item_id": 3,
                "item_title": "running shoes",
                "reason": "size_issue",
                "resolution": "refund",
            }
        )
        node = await _after_resolution(deps, fm)
        assert node["name"] == "confirm_mutation"
        gate = fm.state[GATE_STATE_KEY]
        await gate.on_fit(True)
        assert fm.current_node == "mutation_done"
        content = fm.set_nodes[-1]["task_messages"][0]["content"]
        assert "RET-" in content and "2599 rupees" in content
