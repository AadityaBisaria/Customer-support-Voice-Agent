"""Regression conversations run against the real seeded store without inference."""

import pytest

from flows.commands import CommandBatch, CompilerBatch, vertex_compiler_schema
from flows.runtime import CommandRuntime
from flows.slots import DynamicOrderReferenceSlot
from flows.stack import FlowStack
from rag.index import QAEntry
from store.domain import OrderId, OrderStatus, PhoneNumber


def batch(*commands):
    return CommandBatch.model_validate({'commands': commands})


def start(flow='order_status', request='a', mode='queue'):
    return {'type': 'START_FLOW', 'request_id': request, 'flow_id': flow, 'mode': mode}


def slot(key, value, target='a'):
    return {'type': 'SET_SLOT', 'target_request_id': target, 'key': key, 'value': value}


def resolve(action, query=None, request='resolve'):
    command = {'type': 'RESOLVE_INTENT', 'request_id': request, 'action': action}
    if query is not None:
        command['entity_query'] = query
    return command


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


def test_resolve_intent_cannot_carry_a_live_slot_or_id():
    with pytest.raises(ValueError):
        batch({
            **resolve('cancel_order', 'kurta'),
            'order_ref': 'AMZ-1001',
        })


def test_compiler_protocol_rejects_backend_mutation_commands():
    with pytest.raises(ValueError):
        CompilerBatch.model_validate({'commands': [start('cancel_order')]})


def test_vertex_schema_exposes_only_public_compiler_commands():
    variants = vertex_compiler_schema()['properties']['commands']['items']['oneOf']
    types = {variant['properties']['type']['enum'][0] for variant in variants}
    assert types == {'RESOLVE_INTENT', 'FAQ_ANSWER', 'CLARIFY', 'END_CALL'}
    clarify = next(variant for variant in variants
                   if variant['properties']['type']['enum'] == ['CLARIFY'])
    assert clarify['additionalProperties'] is False
    assert 'request_id' not in clarify['properties']


def test_policy_retrieval_requires_a_clear_winner():
    from flows.command_processor import CommandProcessor

    first = QAEntry('exchange', 'exchange?', 'grounded exchange answer')
    second = QAEntry('return', 'return?', 'other answer')

    class Index:
        def __init__(self, results):
            self.results = results

        def embed_query(self, text):
            return text

        def search(self, query, top_k=3):
            return self.results

    accepted = CommandProcessor(llm=None, deps=None,
                                index=Index([(first, 0.72), (second, 0.55)]))
    ambiguous = CommandProcessor(llm=None, deps=None,
                                 index=Index([(first, 0.65), (second, 0.60)]))
    assert accepted.retrieve('exchange policy') == {'exchange': 'grounded exchange answer'}
    assert ambiguous.retrieve('exchange policy') == {}


async def test_vikram_request_survives_auth_and_reports_empty(deps):
    runtime = CommandRuntime(deps)
    assert 'registered mobile' in await runtime.apply(batch(start()))
    assert [f.flow_id for f in runtime.stack.frames] == ['order_status', 'verify_phone']
    answer = await runtime.shortcut('9111111111')
    assert 'कोई orders नहीं' in answer
    assert not runtime.stack.frames
    assert 'कोई orders नहीं' in await runtime.apply(batch(start('cancel_order', 'b')))


async def test_unverified_intent_suspends_before_lookup_then_resumes_after_identity_check(deps):
    runtime = CommandRuntime(deps)
    prompt = await runtime.apply(batch(resolve('cancel_order', 'kurta')))
    assert 'identity verify' in prompt
    assert 'registered mobile number' in prompt
    assert runtime.stack.active.flow_id == 'verify_phone'
    assert runtime.state['pending_intent']['entity_query'] == 'kurta'
    answer = await runtime.shortcut('9876543210')
    assert 'Anarkali kurta set' in answer
    assert 'delivered' in answer
    assert runtime.current_node == 'triage'


async def test_hindi_inflected_item_resolves_after_verification(deps):
    runtime = CommandRuntime(deps)
    await runtime.apply(batch(resolve('cancel_order', 'कुर्ते')))
    answer = await runtime.shortcut('9876543210')
    assert 'Anarkali kurta set' in answer
    assert 'delivered' in answer
    assert runtime.current_node == 'triage'
    assert not runtime.stack.frames


async def test_unmatched_post_verification_entity_does_not_default_to_eligible_order(deps):
    runtime = CommandRuntime(deps)
    await runtime.apply(batch(resolve('cancel_order', 'umbrella')))
    answer = await runtime.shortcut('9876543210')
    assert 'umbrella' in answer
    assert 'phone case' in answer
    assert runtime.current_node == 'triage'
    assert not runtime.stack.frames


async def test_wrong_phone_does_not_resume_business_flow(deps):
    runtime = CommandRuntime(deps)
    await runtime.apply(batch(start('cancel_order')))
    await runtime.shortcut('9999999991')
    await runtime.shortcut('9999999992')
    answer = await runtime.shortcut('9999999993')
    assert 'verify नहीं हुआ' in answer
    assert deps.customer is None
    assert not runtime.stack.frames


async def test_partial_phone_number_is_reprompted_without_compiler_fallback(deps):
    runtime = CommandRuntime(deps)
    await runtime.apply(batch(start('return_order')))
    answer = await runtime.shortcut('987654321')
    assert '9 digits' in answer
    assert runtime.stack.active.flow_id == 'verify_phone'
    assert runtime.deps.verify_attempts == 1


async def test_unknown_valid_phone_is_described_as_no_account(deps):
    runtime = CommandRuntime(deps)
    await runtime.apply(batch(start('return_order')))
    answer = await runtime.shortcut('9999999991')
    assert 'कोई account नहीं मिला' in answer
    assert 'registered mobile number' in answer
    assert runtime.stack.active.flow_id == 'verify_phone'


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


@pytest.mark.parametrize('answer', ['हाँ cancel कर दो', 'हाँ cancel कर दीजिए', 'haan cancel kar do'])
async def test_confirmation_consumes_action_filler_before_dynamic_routing(deps, answer):
    runtime = await verified(deps)
    await runtime.apply(batch(start('cancel_order'), slot('order_ref', 'AMZ-1004')))
    result = await runtime.shortcut(answer)
    assert 'cancelled' in result
    assert (await deps.store.order_with_items(OrderId('AMZ-1004'))).status == OrderStatus.CANCELLED


async def test_correction_invalidates_old_confirmation(deps):
    runtime = await verified(deps)
    await runtime.apply(batch(start('cancel_order'), slot('order_ref', 'AMZ-1004')))
    key = runtime.state['pending'].idempotency_key
    refusal = await runtime.apply(batch(slot('order_ref', 'shoes')))
    assert 'pending' not in runtime.state
    assert (await deps.store.order_with_items(OrderId('AMZ-1004'))).status == OrderStatus.PLACED
    assert 'cancel' in refusal
    await runtime.apply(batch(start('cancel_order'), slot('order_ref', 'AMZ-1004')))
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


async def test_cancel_preempts_open_status_picker_and_keeps_product_reference(deps):
    runtime = await verified(deps)
    from flows.nodes.journeys import make_select_order
    from flows.stack import FlowFrame

    # The status picker exists after a caller asks about a particular order.
    runtime.stack.frames.append(FlowFrame('picker', 'order_status'))
    await runtime.set_node_from_config(await make_select_order(deps, runtime, 'status'))
    answer = await runtime.apply(batch(start('cancel_order', 'cancel'), slot('order_ref', 'phone case', 'cancel')))
    assert runtime.stack.active.request_id == 'cancel'
    assert not runtime.stack.queue
    assert runtime.current_node == 'confirm_mutation'
    assert 'AMZ-1004' in answer


async def test_cancel_preempts_return_picker_and_drops_stale_retry_frames(deps):
    runtime = await verified(deps, '9123456789')
    from flows.nodes.journeys import make_select_order
    from flows.stack import FlowFrame

    runtime.stack.frames.append(FlowFrame('return_picker', 'return_order'))
    runtime.stack.queue.append(FlowFrame('stale_cancel', 'cancel_order', {'order_ref': 'AMZ-2003'}))
    await runtime.set_node_from_config(await make_select_order(deps, runtime, 'return'))
    answer = await runtime.shortcut('Mujhe apni T-shirt cancel karni hai')
    assert runtime.current_node == 'confirm_mutation'
    assert runtime.stack.active.flow_id == 'cancel_order'
    assert runtime.stack.active.slots['order_ref'] == 'AMZ-2003'
    assert not runtime.stack.queue
    assert 'AMZ-2003' in answer


async def test_policy_refusal_does_not_auto_promote_stale_queue(deps):
    runtime = await verified(deps, '9123456789')
    from flows.stack import FlowFrame

    runtime.stack.queue.append(FlowFrame('stale_cancel', 'cancel_order', {'order_ref': 'AMZ-2003'}))
    answer = await runtime.apply(batch(start('cancel_order'), slot('order_ref', 'AMZ-2001')))
    assert 'mixer grinder' in answer
    assert 'AMZ-2003' not in answer
    assert not runtime.stack.frames
    assert not runtime.stack.queue


@pytest.mark.parametrize('reference', ['AMZ-1004', 'AMZ 1004', 'amz1004', 'A M Z one zero zero four'])
async def test_order_id_variants_resolve_to_live_menu_choice(deps, reference):
    runtime = await verified(deps)
    await runtime.apply(batch(start('cancel_order')))
    answer = await runtime.shortcut(reference)
    assert runtime.current_node == 'confirm_mutation'
    assert 'AMZ-1004' in answer


async def test_personal_action_and_live_title_bypass_compiler(deps):
    runtime = await verified(deps)
    answer = await runtime.shortcut('Mujhe apna phone case cancel karna hai')
    assert runtime.current_node == 'confirm_mutation'
    assert 'AMZ-1004' in answer


async def test_resolve_intent_binds_a_live_line_item_before_starting_exchange(deps):
    runtime = await verified(deps)
    answer = await runtime.apply(batch(resolve('exchange_item', 'kurta')))
    assert runtime.stack.active.flow_id == 'exchange_item'
    assert runtime.stack.active.slots['order_ref'] == 'AMZ-1001'
    assert runtime.stack.active.slots['item_ref'] == '1'
    assert runtime.stack.active.slots['resolution'] == 'exchange'
    assert runtime.current_node == 'select_reason'
    assert 'Anarkali kurta set' in answer


async def test_active_exchange_reason_and_variant_are_fit_before_routing(deps):
    runtime = await verified(deps)
    await runtime.apply(batch(resolve('exchange_item', 'kurta')))
    answer = await runtime.shortcut('size issue hai, mujhe large chahiye')
    assert runtime.current_node == 'confirm_mutation'
    assert runtime.stack.active.slots['reason'] == 'size_issue'
    assert runtime.stack.active.slots['variant_ref'] == 'l'
    assert runtime.deps.wip['new_variant_label'] == 'L teal'
    assert 'exchange' in answer


@pytest.mark.parametrize('utterance', ['refund दे दो', 'refund नहीं, exchange', 'refund ఏదో'])
async def test_resolution_uses_semantic_live_option_fitting(deps, utterance):
    runtime = await verified(deps)
    await runtime.apply(batch(start('return_order'), slot('order_ref', 'AMZ-1001'),
                              slot('item_ref', '1'), slot('reason', 'size_issue')))
    answer = await runtime.shortcut(utterance)
    expected = 'exchange' if utterance == 'refund नहीं, exchange' else 'refund'
    assert runtime.current_node == ('select_exchange_variant' if expected == 'exchange' else 'confirm_mutation')
    assert runtime.stack.active.slots['resolution'] == expected
    assert answer


async def test_order_id_starts_an_order_scoped_item_selection(deps):
    runtime = await verified(deps)
    await runtime.apply(batch(resolve('exchange_item', 'AMZ 1001')))
    assert runtime.stack.active.slots['order_ref'] == 'AMZ-1001'
    assert 'item_ref' not in runtime.stack.active.slots
    assert runtime.current_node == 'select_item'


async def test_resolve_intent_refuses_delivered_kurta_cancel_and_returns_to_triage(deps):
    runtime = await verified(deps)
    answer = await runtime.apply(batch(resolve('cancel_order', 'kurta')))
    assert 'Anarkali kurta set' in answer
    assert 'delivered' in answer
    assert 'return' in answer and 'exchange' in answer
    assert runtime.current_node == 'triage'
    assert not runtime.stack.frames
    assert 'pending' not in runtime.state


async def test_same_flow_order_picker_correction_reresolves_full_account(deps):
    runtime = await verified(deps)
    await runtime.apply(batch(start('cancel_order')))
    assert runtime.current_node == 'select_order_cancel'
    answer = await runtime.apply(batch(resolve('cancel_order', 'कुर्ता')))
    assert 'Anarkali kurta set' in answer
    assert 'delivered' in answer
    assert 'phone case' not in answer
    assert runtime.current_node == 'triage'
    assert not runtime.stack.frames


async def test_first_uses_the_presented_menu_order_not_chronology(deps):
    customer = await deps.store.customer_by_phone(PhoneNumber.parse('9876543210'))
    orders = await deps.store.orders_for_customer(customer.customer_id)
    slot_surface = DynamicOrderReferenceSlot(orders, presentation_ids=['AMZ-1001', 'AMZ-1004'])
    assert slot_surface.match('first one').order_id == 'AMZ-1001'
    assert slot_surface.match('latest').order_id == 'AMZ-1004'


async def test_cancel_delivered_order_is_refused_without_confirmation(deps):
    runtime = await verified(deps)
    answer = await runtime.apply(batch(start('cancel_order'), slot('order_ref', 'running shoes')))
    assert 'deliver' in answer
    assert 'confirm_mutation' != runtime.current_node
    assert 'pending' not in runtime.state


async def test_order_summary_is_natural_and_has_no_raw_ids(deps):
    runtime = await verified(deps)
    answer = await runtime.apply(batch(start('order_status')))
    assert '4 orders' in answer
    assert 'phone case' in answer
    assert 'AMZ-' not in answer


async def test_two_requests_run_in_order(deps):
    runtime = await verified(deps)
    answer = await runtime.apply(batch(start(), slot('order_ref', 'AMZ-1002'),
                                      start('cancel_order', 'b'), slot('order_ref', 'AMZ-1004', 'b')))
    assert answer.index('delivered') < answer.index('AMZ-1004')
    assert runtime.current_node == 'confirm_mutation'


async def test_refund_empty_is_not_order_empty(deps):
    runtime = await verified(deps, '9111111111')
    assert 'refund record नहीं' in await runtime.apply(batch(start('refund_status')))


async def test_empty_account_personal_follow_up_skips_compiler(deps):
    runtime = await verified(deps, '9111111111')
    answer = await runtime.shortcut('Mujhe apna order cancel karna hai')
    assert 'कोई orders नहीं' in answer


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
            return batch(resolve('order_status')).model_dump_json()

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
