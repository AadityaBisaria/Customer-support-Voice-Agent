"""Regression conversations run against the real seeded store without inference."""

import pytest

from flows.commands import CommandBatch
from flows.runtime import CommandRuntime
from flows.stack import FlowStack
from store.domain import OrderId, OrderStatus, PhoneNumber


def batch(*commands):
    return CommandBatch.model_validate({'commands': commands})


def start(flow='order_status', request='a', mode='queue'):
    return {'type': 'START_FLOW', 'request_id': request, 'flow_id': flow, 'mode': mode}


def slot(key, value, target='a'):
    return {'type': 'SET_SLOT', 'target_request_id': target, 'key': key, 'value': value}


async def verified(deps, number='9876543210'):
    deps.customer = await deps.store.customer_by_phone(PhoneNumber.parse(number))
    return CommandRuntime(deps)


def test_explicit_targets_isolate_queued_slots():
    stack = FlowStack()
    stack.apply(batch(start(), slot('order_ref', 'shoes'),
                      start('cancel_order', 'b'), slot('order_ref', 'kurta', 'b')))
    assert stack.active.slots['order_ref'] == 'shoes'
    assert stack.queue[0].slots['order_ref'] == 'kurta'


def test_invalid_batch_is_atomic():
    stack = FlowStack()
    with pytest.raises(ValueError):
        stack.apply(batch(start(), slot('reason', 'damaged')))
    assert not stack.frames


def test_llm_cannot_confirm():
    with pytest.raises(ValueError):
        batch({'type': 'SET_SLOT', 'target_request_id': 'a', 'key': 'confirmed', 'value': 'yes'})


async def test_vikram_request_survives_auth_and_reports_empty(deps):
    runtime = CommandRuntime(deps)
    assert 'registered mobile' in await runtime.apply(batch(start()))
    assert [f.flow_id for f in runtime.stack.frames] == ['order_status', 'verify_phone']
    answer = await runtime.shortcut('9111111111')
    assert 'कोई orders नहीं' in answer
    assert not runtime.stack.frames
    assert 'कोई orders नहीं' in await runtime.apply(batch(start('cancel_order', 'b')))


async def test_wrong_phone_does_not_resume_business_flow(deps):
    runtime = CommandRuntime(deps)
    await runtime.apply(batch(start('cancel_order')))
    await runtime.shortcut('9999999991')
    answer = await runtime.shortcut('9999999992')
    assert 'verify नहीं हुआ' in answer
    assert deps.customer is None
    assert not runtime.stack.frames


async def test_cancel_confirmation_executes_only_after_whole_yes(deps):
    runtime = await verified(deps)
    answer = await runtime.apply(batch(start('cancel_order'), slot('order_ref', 'AMZ-1004')))
    assert 'AMZ-1004' in answer
    assert runtime.current_node == 'confirm_mutation'
    assert await runtime.shortcut('yes but cancel the shoes instead') is None
    assert (await deps.store.order_with_items(OrderId('AMZ-1004'))).status == OrderStatus.PLACED
    answer = await runtime.shortcut('yes')
    assert 'cancelled' in answer
    assert (await deps.store.order_with_items(OrderId('AMZ-1004'))).status == OrderStatus.CANCELLED
    assert await runtime.shortcut('yes') is None


async def test_correction_invalidates_old_confirmation(deps):
    runtime = await verified(deps)
    await runtime.apply(batch(start('cancel_order'), slot('order_ref', 'AMZ-1004')))
    key = runtime.state['pending'].idempotency_key
    await runtime.apply(batch(slot('order_ref', 'shoes')))
    assert 'pending' not in runtime.state
    assert (await deps.store.order_with_items(OrderId('AMZ-1004'))).status == OrderStatus.PLACED
    await runtime.apply(batch(slot('order_ref', 'AMZ-1004')))
    assert runtime.state['pending'].idempotency_key != key


async def test_read_digression_resumes_return_slots(deps):
    runtime = await verified(deps)
    await runtime.apply(batch(start('return_order'), slot('order_ref', 'AMZ-1001')))
    before = dict(runtime.stack.active.slots)
    answer = await runtime.apply(batch(start('order_status', 'b', 'interrupt'),
                                      slot('order_ref', 'AMZ-1002', 'b')))
    assert 'delivered' in answer
    assert runtime.stack.active.request_id == 'a'
    assert runtime.stack.active.slots == before


async def test_two_requests_run_in_order(deps):
    runtime = await verified(deps)
    answer = await runtime.apply(batch(start(), slot('order_ref', 'AMZ-1002'),
                                      start('cancel_order', 'b'), slot('order_ref', 'AMZ-1004', 'b')))
    assert answer.index('delivered') < answer.index('AMZ-1004')
    assert runtime.current_node == 'confirm_mutation'


async def test_refund_empty_is_not_order_empty(deps):
    runtime = await verified(deps, '9111111111')
    assert 'refund record नहीं' in await runtime.apply(batch(start('refund_status')))


async def test_faq_cannot_invent_source(deps):
    runtime = CommandRuntime(deps)
    with pytest.raises(ValueError):
        await runtime.apply(batch({'type': 'FAQ_ANSWER', 'source_id': 'invented'}), sources={})


@pytest.mark.parametrize('flow,phone,commands', [
    ('return_order', '9876543210', [slot('order_ref', 'AMZ-1002'),
                                  slot('reason', 'damaged'), slot('resolution', 'refund')]),
    ('exchange_item', '9876543210', [slot('order_ref', 'AMZ-1001'),
                                   slot('item_ref', 'kurta'), slot('reason', 'size_issue'),
                                   slot('resolution', 'exchange'), slot('variant_ref', 'L teal')]),
    ('reschedule_delivery', '9123456789', [slot('order_ref', 'AMZ-2002'),
                                         slot('days_ahead', '2'), slot('slot', 'morning')]),
    ('missing_delivery', '9000000001', [slot('order_ref', 'AMZ-3006'),
                                      slot('dispute_type', 'item_not_received')]),
])
async def test_remaining_journeys_reach_and_execute_confirmation(deps, flow, phone, commands):
    runtime = await verified(deps, phone)
    await runtime.apply(batch(start(flow), *commands))
    assert runtime.current_node == 'confirm_mutation', runtime.node
    answer = await runtime.shortcut('yes')
    assert "couldn't" not in answer, answer
    assert not runtime.stack.frames


async def test_one_inference_for_initial_request_zero_for_phone(deps, monkeypatch):
    from flows.command_processor import CommandProcessor

    class Model:
        calls = 0
        async def run_inference(self, *args, **kwargs):
            self.calls += 1
            return batch(start()).model_dump_json()

    model = Model()
    processor = CommandProcessor(llm=model, deps=deps, index=None)
    monkeypatch.setattr(processor, 'retrieve', lambda text: {})
    # This test exercises the processor turn logic without starting a worker.
    import asyncio
    monkeypatch.setattr(processor, 'create_task', asyncio.create_task)
    spoken = []
    async def speak(text, generation):
        spoken.append(text)
    monkeypatch.setattr(processor, 'speak', speak)
    await processor.handle('मेरा order क्या है?', [], 0)
    await processor.handle('9111111111', [], 0)
    assert model.calls == 1
    assert 'registered mobile' in spoken[0]
    assert 'कोई orders नहीं' in spoken[1]
