"""Execute validated commands using the existing journey factories and store guards."""

import re
import unicodedata
from uuid import uuid4

from dialogue.phone import PhoneSlot, extract_digits, strip_country_prefix
from dialogue.results import Confidence, Fit
from dialogue.utterance import Utterance
from store.domain import OrderStatus, PhoneNumber

from .commands import AbortFlow, Clarify, CommandBatch, EndCall, PolicyAnswer, ResolveIntent
from .gate_processor import GATE_STATE_KEY, evaluate_gate
from .slots import (DynamicOrderReferenceSlot, canonical_order_id, compact,
                    order_reference_matches, reference_tokens)
from .speech import empty_speech
from .stack import FlowFrame, FlowStack

PURPOSES = {'order_status': 'status', 'cancel_order': 'cancel', 'return_order': 'return',
            'exchange_item': 'return', 'reschedule_delivery': 'reschedule',
            'missing_delivery': 'dispute'}
SLOT_TO_TOOL = {'select_order': ('order_ref', 'order_id'),
                'select_item': ('item_ref', 'order_item_id'),
                'select_reason': ('reason', 'reason'),
                'select_resolution': ('resolution', 'resolution'),
                'select_variant': ('variant_ref', 'variant_id'),
                'select_reschedule_date': ('days_ahead', 'days_ahead'),
                'select_dispute_reason': ('dispute_type', 'dispute_type')}
TERMINAL = {'nothing_here', 'wrap', 'order_status_report', 'refund_status_report',
            'mutation_done', 'mutation_failed', 'mutation_cancelled', 'policy_refusal',
            'order_summary_report'}
PHONE_PROMPT = 'कृपया अपना registered mobile number बताइए।'
CLARIFICATION = 'मैं आपकी बात समझना चाहता हूँ। आपको किस order में क्या मदद चाहिए?'


def norm(text):
    # Preserve Devanagari vowel signs/nasalization (Unicode combining marks).
    return ''.join(c for c in text.casefold() if c.isspace() or c == '-' or
                   unicodedata.category(c)[0] in {'L', 'N', 'M'}).strip()


def personal_order_request(text):
    """Recognize an account operation without confusing it with a policy FAQ."""
    words = set(text.split())
    personal = {'my', 'mera', 'mere', 'meri', 'apna', 'apne', 'apni', 'mujhe'}
    operation = {
        'order', 'orders', 'cancel', 'return', 'exchange', 'replace', 'replacement',
        'reschedule', 'delivery', 'tracking', 'status', 'refund',
    }
    return bool(words & personal and words & operation)


def transactional_request(text):
    words = set(text.split())
    return bool(words & {'cancel', 'return', 'exchange', 'replace', 'replacement', 'reschedule', 'dispute'})


def confirmation_correction(text):
    """A mixed answer must not be mistaken for consent to the armed mutation."""
    words = set(norm(text).split())
    return bool(words & {'but', 'instead', 'lekin', 'magar', 'par', 'dusra', 'another'})


def confirmation_affirmative(text):
    """Accept harmless action filler without weakening correction safety."""
    normalized = norm(text)
    negative = {'no', 'nahi', 'nahin', 'mat', 'nahi', 'नहीं', 'मत', 'rehne', 'stop'}
    words = set(normalized.split())
    if words & negative:
        return False
    yes = {'yes', 'haan', 'han', 'haa', 'ji', 'bilkul', 'zaroor', 'sure', 'हाँ', 'हां', 'जी', 'बिलकुल'}
    action = ('kar do', 'kar dijiye', 'kardo', 'कर दो', 'कर दीजिए')
    return bool(words & yes) or any(phrase in normalized for phrase in action)


def fit_reason(text):
    """Closed-set reason fitting for the active return/exchange node."""
    normalized = norm(text)
    surfaces = {
        'size_issue': ('size', 'fitting', 'fit issue', 'large', 'small', 'medium', 'xl', 'xs',
                       'chhota', 'bada', 'छोटा', 'बड़ा'),
        'damaged': ('damage', 'damaged', 'broken', 'tuta', 'kharab', 'टूटा', 'खराब'),
        'defective': ('defect', 'defective', 'not working', 'खराबी'),
        'wrong_item': ('wrong item', 'different item', 'galat', 'गलत'),
        'missing_parts': ('missing part', 'missing accessory', 'parts missing', 'गायब'),
        'not_needed': ('not needed', 'dont need', "don't need", 'nahi chahiye', 'नहीं चाहिए'),
    }
    return next((reason for reason, aliases in surfaces.items()
                 if any(alias in normalized for alias in aliases)), None)


def fit_variant_reference(text):
    """Keep a spoken size as a deferred local variant selection."""
    normalized = norm(text)
    aliases = {
        'xs': ('xs', 'extra small'), 's': ('small',), 'm': ('medium',),
        'l': ('large',), 'xl': ('xl', 'extra large'), 'xxl': ('xxl',),
    }
    return next((size for size, terms in aliases.items()
                 if any(term in normalized.split() or term in normalized for term in terms)), None)


_RESOLUTION_SURFACES = {
    'refund': (
        ('refund',), ('refunds',), ('refound',), ('refund', 'de', 'do'),
        ('paise', 'wapas'), ('paisa', 'wapas'), ('पैसे', 'वापस'), ('पैसा', 'वापस'),
    ),
    'exchange': (
        ('exchange',), ('replacement',), ('replace',), ('swap',),
        ('size', 'change'), ('size', 'badalna'), ('साइज़', 'बदलना'),
    ),
}
_RESOLUTION_NEGATIONS = frozenset({'no', 'not', 'nahi', 'nahin', 'nhi', 'mat', 'नहीं', 'मत'})


def fit_resolution(text, valid_options):
    """Choose one live resolution from a natural utterance, respecting negation."""
    # Keep Devanagari tokens for phrases such as "पैसे वापस". Other-script
    # ASR noise is harmless: an intact English anchor such as "refund" still
    # survives and is enough to select the constrained live enum.
    tokens = re.findall(r'[a-z]+|[\u0900-\u097f]+', norm(text))
    hits = set()
    for option in valid_options:
        for surface in _RESOLUTION_SURFACES.get(option, ((option,),)):
            width = len(surface)
            for start in range(len(tokens) - width + 1):
                if tuple(tokens[start:start + width]) != surface:
                    continue
                before = tokens[max(0, start - 3):start]
                after = tokens[start + width:start + width + 2]
                prior_is_other_choice = (len(before) >= 2 and
                                         before[-2] in {'refund', 'refunds', 'refound',
                                                        'exchange', 'replacement', 'replace', 'swap'})
                negated_before = before[-1:] and before[-1] in _RESOLUTION_NEGATIONS and not prior_is_other_choice
                negated_after = not _RESOLUTION_NEGATIONS.isdisjoint(after)
                if not negated_before and not negated_after:
                    hits.add(option)
    return next(iter(hits)) if len(hits) == 1 else None


def direct_transaction_flow(text):
    """High-confidence personal action triggers; policy questions stay semantic."""
    words = set(text.split())
    personal = {'my', 'mera', 'mere', 'meri', 'apna', 'apne', 'apni', 'mujhe',
                'मेरा', 'मेरे', 'मेरी', 'अपना', 'अपने', 'मुझे'}
    if not words & personal:
        return None
    if 'cancel' in words:
        return 'cancel_order'
    if 'exchange' in words:
        return 'exchange_item'
    if words & {'return', 'replace', 'replacement'}:
        return 'return_order'
    if 'reschedule' in words:
        return 'reschedule_delivery'
    if 'dispute' in words:
        return 'missing_delivery'
    return None


def verification_prompt(flow_id: str | None) -> str:
    prompts = {
        'order_status': 'आपके orders retrieve करने से पहले मुझे आपकी identity verify करनी है। अपना registered mobile number बताइए।',
        'cancel_order': 'Cancellation request check करने से पहले मुझे आपकी identity verify करनी है। अपना registered mobile number बताइए।',
        'return_order': 'Return request शुरू करने से पहले मुझे आपकी identity verify करनी है। अपना registered mobile number बताइए।',
        'exchange_item': 'Exchange options check करने से पहले मुझे आपकी identity verify करनी है। अपना registered mobile number बताइए।',
        'reschedule_delivery': 'Delivery reschedule करने से पहले मुझे आपकी identity verify करनी है। अपना registered mobile number बताइए।',
        'missing_delivery': 'Delivery investigation open करने से पहले मुझे आपकी identity verify करनी है। अपना registered mobile number बताइए।',
        'refund_status': 'Refund details check करने से पहले मुझे आपकी identity verify करनी है। अपना registered mobile number बताइए।',
    }
    return prompts.get(flow_id, PHONE_PROMPT)


class CommandRuntime:
    """Factory-compatible manager; changing a node never invokes the LLM."""

    def __init__(self, deps):
        self.state = {'deps': deps, 'flow_stack': FlowStack()}
        self.deps = deps
        self.current_node = None
        self.node = None
        self.last_speech = ''
        self.ended = False
        self.auth_failed = False

    @property
    def stack(self):
        return self.state['flow_stack']

    def event(self, name, **data):
        if self.deps.transcript:
            self.deps.transcript.log_event(name, **data)

    def invalidate_pending(self, *, reason: str) -> None:
        """An armed idempotency key belongs to exactly one unmodified target."""
        pending = self.state.pop('pending', None)
        self.state.pop(GATE_STATE_KEY, None)
        if pending is not None:
            self.event('pending_invalidated', reason=reason,
                       operation=getattr(pending, 'op', None),
                       idempotency_key=getattr(pending, 'idempotency_key', None))

    async def set_node_from_config(self, node):
        self.node = node
        self.current_node = node['name']
        if self.stack.active:
            self.stack.active.node = node
        self.event('node_entered', node=self.current_node)

    def snapshot(self):
        return {'authenticated': self.deps.customer is not None,
                'frames': self.stack.snapshot(), 'node': self.current_node,
                'choices': (self.node or {}).get('choices', {})}

    def phone_prompt(self):
        suspended = self.stack.frames[-2] if len(self.stack.frames) >= 2 else None
        return verification_prompt(suspended.flow_id if suspended else None)

    async def order_surface(self, *, presentation_ids=None):
        if self.deps.customer is None:
            return None
        orders = await self.deps.store.orders_for_customer(self.deps.customer.customer_id)
        return DynamicOrderReferenceSlot(orders, presentation_ids=presentation_ids)

    async def start_direct_transaction(self, text, flow_id):
        # Deterministic shortcuts deliberately use the same entity-resolution
        # path as the compiler. A spoken product name never becomes a slot.
        # If the local matcher cannot resolve it, let the single compiler
        # normalize the entity phrase instead of clearing the conversation.
        # Before authentication there is no live menu to match against. Let
        # the compiler extract a compact entity phrase ("kurta", not the
        # whole spoken sentence) and let resolve_customer_intent suspend it
        # behind verification.
        if self.deps.customer is None:
            return None
        matches, _ = await self._match_live_item(text)
        if not matches:
            return None
        return await self.resolve_customer_intent(flow_id, text, source='shortcut')

    @staticmethod
    def _query_tokens(query):
        return reference_tokens(query)

    async def _match_live_item(self, query):
        """Resolve the caller's words to a live line item, never an LLM ID."""
        if self.deps.customer is None:
            return (), ()
        orders = await self.deps.store.orders_for_customer(self.deps.customer.customer_id)
        canonical = canonical_order_id(query)
        hits = []
        query_tokens = self._query_tokens(query)
        for summary in orders:
            details = await self.deps.store.items_for_order(summary.order_id)
            if canonical and str(summary.order_id).casefold() == canonical.casefold():
                hits.extend((summary, detail) for detail in details)
                continue
            for detail in details:
                title = detail.product.title.casefold()
                title_tokens = reference_tokens(title)
                full_title = compact(title)
                full_query = compact(query)
                if (full_title and len(full_title) > 3 and full_title in full_query) or (
                    query_tokens and query_tokens <= title_tokens
                ):
                    hits.append((summary, detail))
        return tuple(hits), tuple(orders)

    def _open_triage(self, *, reason):
        """A refusal is informational; it must not trap the caller in another flow."""
        self.invalidate_pending(reason=reason)
        self.stack.frames.clear()
        self.stack.queue.clear()
        self.node = None
        self.current_node = 'triage'
        self.event('intent_refused', reason=reason)

    def _unmatched_entity_message(self, action, entity_query, orders):
        """Explain a true catalogue miss without silently changing targets."""
        eligible_statuses = {
            'cancel_order': {OrderStatus.PLACED},
            'return_order': {OrderStatus.DELIVERED},
            'exchange_item': {OrderStatus.DELIVERED},
            'reschedule_delivery': {OrderStatus.SHIPPED, OrderStatus.OUT_FOR_DELIVERY},
            'missing_delivery': {OrderStatus.DELIVERED},
        }
        candidates = [order for order in orders if order.status in eligible_statuses.get(action, set())]
        self._open_triage(reason='entity_not_found')
        base = (f'मुझे आपके account में "{entity_query}" नहीं मिला। ')
        if len(candidates) == 1:
            order = candidates[0]
            titles = ', '.join(order.item_titles)
            action_word = {
                'cancel_order': 'cancel करने के लिए',
                'return_order': 'return करने के लिए',
                'exchange_item': 'exchange करने के लिए',
                'reschedule_delivery': 'reschedule करने के लिए',
                'missing_delivery': 'investigation के लिए',
            }.get(action, 'help के लिए')
            return (base + f'आपका एक eligible order {titles} है ({order.order_id})। '
                    f'क्या आप उसे {action_word} पूछ रहे हैं?')
        return base + 'कृपया product का नाम या order ID फिर से बताइए।'

    async def resolve_customer_intent(self, action, entity_query, *, source='compiler'):
        """Resolve a raw phrase, then check fresh state before creating any flow."""
        # Identity is the hard boundary for every account-specific action. Do
        # not query the store, infer an empty account, or bind an entity yet.
        if self.deps.customer is None:
            self.state['pending_intent'] = {
                'action': action,
                'entity_query': entity_query,
                'source': source,
            }
            if not (self.stack.active and self.stack.active.flow_id == 'verify_phone'):
                self.stack.frames.clear()
                self.stack.queue.clear()
                self.stack.frames.append(FlowFrame('auth_' + uuid4().hex[:8], 'verify_phone'))
            self.node = None
            self.current_node = 'verify_phone'
            self.event('verification_required', action=action, source=source)
            return verification_prompt(action)
        if action in {'refund_status'} or not entity_query:
            return await self.apply(CommandBatch.model_validate({'commands': [{
                'type': 'START_FLOW', 'request_id': 'resolved_' + uuid4().hex[:8],
                'flow_id': action,
            }]}))

        self.event('entity_resolution_requested', source=source, action=action,
                   entity_query=entity_query)

        # An order reference identifies the order, not an arbitrary line item.
        # Bind it first and let the next, order-scoped item picker ask only
        # when that order has multiple items.  This avoids treating AMZ-1001
        # as a global two-item ambiguity.
        canonical = canonical_order_id(entity_query)
        if canonical:
            orders = await self.deps.store.orders_for_customer(self.deps.customer.customer_id)
            order_summary = next((summary for summary in orders
                                  if str(summary.order_id).casefold() == canonical.casefold()), None)
            if order_summary is not None:
                request_id = 'resolved_' + uuid4().hex[:8]
                return await self.apply(CommandBatch.model_validate({'commands': [
                    {'type': 'START_FLOW', 'request_id': request_id, 'flow_id': action},
                    {'type': 'SET_SLOT', 'target_request_id': request_id,
                     'key': 'order_ref', 'value': str(order_summary.order_id)},
                ]}))

        matches, orders = await self._match_live_item(entity_query)
        self.event('entity_resolution_candidates', action=action,
                   entity_query=entity_query, candidate_orders=len(orders),
                   matches=[{
                       'order_id': str(summary.order_id),
                       'order_item_id': int(detail.item.order_item_id),
                   } for summary, detail in matches])
        if not matches:
            if not orders:
                return empty_speech(self.deps, 'orders')
            return self._unmatched_entity_message(action, entity_query, orders)
        if len(matches) > 1:
            labels = ' और '.join(
                f'{detail.product.title} ({summary.status.value})'
                for summary, detail in matches[:3]
            )
            self._open_triage(reason='entity_ambiguous')
            return f'मुझे {labels} के multiple matches मिले हैं। आप किस वाले की बात कर रहे हैं?'

        summary, detail = matches[0]
        order = await self.deps.store.order_with_items(summary.order_id)
        title = detail.product.title
        status = order.status.value
        self.event('intent_resolved', source=source, action=action,
                   order_id=str(order.order_id), order_item_id=int(detail.item.order_item_id))

        # This is an authoritative pre-action check. Store mutations repeat
        # their own checks at execution time, closing the stale-read window.
        if action == 'cancel_order' and status != 'placed':
            self._open_triage(reason='cancel_ineligible')
            if status == 'delivered':
                return (f'आपका {title} already delivered है, इसलिए इसे cancel नहीं किया जा सकता। '
                        'अगर item eligible है तो return या exchange options check किए जा सकते हैं।')
            if status == 'cancelled':
                return f'आपका {title} order पहले ही cancelled है। मैं उसका refund status check कर सकता हूँ।'
            return f'आपका {title} dispatch हो चुका है, इसलिए cancellation available नहीं है।'
        if action in {'return_order', 'exchange_item'} and status != 'delivered':
            self._open_triage(reason='return_before_delivery')
            return f'आपका {title} अभी delivered नहीं है, इसलिए return या exchange अभी शुरू नहीं हो सकता।'
        if action == 'reschedule_delivery' and status not in {'shipped', 'out_for_delivery'}:
            self._open_triage(reason='reschedule_ineligible')
            return f'आपके {title} के लिए कोई active shipment नहीं है जिसे reschedule किया जा सके।'
        if action == 'missing_delivery' and status != 'delivered':
            self._open_triage(reason='dispute_before_delivery')
            return f'आपका {title} अभी delivered marked नहीं है, इसलिए missing-delivery investigation शुरू नहीं हो सकती।'

        request_id = 'resolved_' + uuid4().hex[:8]
        commands = [
            {'type': 'START_FLOW', 'request_id': request_id, 'flow_id': action},
            {'type': 'SET_SLOT', 'target_request_id': request_id,
             'key': 'order_ref', 'value': str(order.order_id)},
        ]
        if action in {'return_order', 'exchange_item'}:
            commands.append({'type': 'SET_SLOT', 'target_request_id': request_id,
                             'key': 'item_ref', 'value': str(int(detail.item.order_item_id))})
        return await self.apply(CommandBatch.model_validate({'commands': commands}))

    async def fit_active_order_reference(self, text):
        if not (self.node and self.stack.active and self.current_node.startswith('select_order_')):
            return None
        tool = next((tool for tool in self.node.get('functions', []) if tool.name == 'select_order'), None)
        if tool is None:
            return None
        choices = tool.properties['order_id'].get('enum', [])
        surface = await self.order_surface(presentation_ids=choices)
        if surface is None:
            return None
        # The active menu may intentionally contain only eligible orders.
        permitted = tuple(candidate for candidate in surface.candidates if candidate.order_id in choices)
        outcome = DynamicOrderReferenceSlot.from_candidates(permitted).match(text)
        if outcome.matched:
            self.event('reference_resolved', source='dynamic_menu', order_id=outcome.order_id)
            self.stack.active.slots['order_ref'] = outcome.order_id
            await self.invoke(tool, {'order_id': outcome.order_id})
            return await self.advance_slots()
        if outcome.ambiguous:
            labels = ' और '.join(candidate.label for candidate in outcome.ambiguous[:3])
            self.event('reference_ambiguous', source='dynamic_menu', candidates=[
                candidate.order_id for candidate in outcome.ambiguous
            ])
            return f'मुझे {labels} के multiple orders दिख रहे हैं। आप किस वाले की बात कर रहे हैं?'
        return None

    async def fit_active_node_slots(self, text):
        """Active frames consume their declared slots before global routing."""
        if not (self.node and self.stack.active):
            return None
        if self.current_node == 'select_resolution':
            tool = next((tool for tool in self.node.get('functions', [])
                         if tool.name == 'select_resolution'), None)
            choices = tool.properties['resolution'].get('enum', []) if tool else []
            resolution = fit_resolution(text, choices)
            if resolution is None:
                return None
            self.stack.active.slots['resolution'] = resolution
            self.event('slot_fit', node=self.current_node, resolution=resolution)
            return await self.advance_slots()
        if self.current_node == 'select_reason':
            reason = fit_reason(text)
            if reason is None:
                return None
            self.stack.active.slots['reason'] = reason
            # A caller commonly answers "size issue, large" in one turn.
            if variant := fit_variant_reference(text):
                self.stack.active.slots['variant_ref'] = variant
            self.event('slot_fit', node=self.current_node, reason=reason,
                       variant=self.stack.active.slots.get('variant_ref'))
            return await self.advance_slots()
        return await self.fit_active_order_reference(text)

    async def ineligible_order_message(self, frame):
        """Render a factual refusal for a real but unsuitable order reference."""
        reference = frame.slots.get('order_ref')
        if not reference or self.deps.customer is None:
            return None
        orders = await self.deps.store.orders_for_customer(self.deps.customer.customer_id)
        matches = order_reference_matches(reference, orders)
        if len(matches) != 1:
            return None
        order = next(order for order in orders if str(order.order_id) == matches[0])
        frame.slots['order_ref'] = matches[0]
        title = order.item_titles[0] if order.item_titles else 'यह order'
        if frame.flow_id == 'cancel_order' and order.status.value != 'placed':
            if order.status.value == 'delivered':
                return f'यह {title} पहले ही deliver हो चुका है, इसलिए इसे cancel नहीं किया जा सकता। अगर item eligible है, मैं return options check कर सकता हूँ।'
            return f'यह {title} dispatch हो चुका है, इसलिए cancellation available नहीं है। Delivery के बाद मैं return eligibility check कर सकता हूँ।'
        if frame.flow_id in {'return_order', 'exchange_item'} and order.status.value != 'delivered':
            return f'यह {title} अभी deliver नहीं हुआ है, इसलिए return या exchange अभी available नहीं है।'
        if frame.flow_id == 'reschedule_delivery' and order.status.value not in {'shipped', 'out_for_delivery'}:
            return f'इस {title} के लिए कोई active shipment नहीं है जिसे reschedule किया जा सके।'
        if frame.flow_id == 'missing_delivery' and order.status.value != 'delivered':
            return f'इस {title} के लिए missing-delivery investigation तभी खोली जा सकती है जब order delivered हो।'
        return None

    async def shortcut(self, text):
        normalized = norm(text)
        if normalized in {'repeat', 'repeat that', 'phir se', 'दोबारा', 'फिर से बोलिए'}:
            return self.last_speech
        if normalized in {'hmm', 'achha', 'अच्छा'}:
            return ''
        # An empty account is a store fact, not an LLM judgement. A clearly
        # personal order request is answered immediately; a generic FAQ such
        # as "what is the return policy" still reaches the grounded compiler.
        if self.deps.customer and not self.stack.active and personal_order_request(normalized):
            orders = await self.deps.store.orders_for_customer(self.deps.customer.customer_id)
            if not orders:
                kind = 'refunds' if 'refund' in normalized.split() else 'orders'
                return empty_speech(self.deps, kind)
        if self.stack.active and self.stack.active.flow_id == 'verify_phone':
            prompt = self.phone_prompt()
            utterance = Utterance.from_text(text)
            result = await PhoneSlot(name='phone', prompt=prompt).fit_utterance(
                utterance, self.deps.fit_ctx())
            if not isinstance(result, Fit) or result.confidence != Confidence.HIGH:
                # Digit-like input is owned by the verification state machine.
                # It must never fall through to the general compiler, which
                # cannot know whether a partial number was an ASR drop or a
                # caller's account question.
                digits = strip_country_prefix(extract_digits(utterance.tokens))
                if digits:
                    self.deps.verify_attempts += 1
                    self.event('verification_invalid_length', digits=len(digits),
                               attempts=self.deps.verify_attempts)
                    if self.deps.verify_attempts >= 3:
                        self.stack.frames.clear()
                        self.stack.queue.clear()
                        self.state.pop('pending_intent', None)
                        self.deps.verify_attempts = 0
                        self.auth_failed = True
                        return ('आपका account verify नहीं हो पाया। मैं general policy questions में '
                                'मदद कर सकता हूँ।')
                    return (f'आपने {len(digits)} digits बताए हैं। Registered mobile number 10 digits '
                            f'का होना चाहिए। कृपया फिर से बताइए।')
                return None
            customer = await self.deps.store.customer_by_phone(PhoneNumber.parse(result.value))
            if customer is None:
                self.deps.verify_attempts += 1
                self.event('verification_account_not_found', attempts=self.deps.verify_attempts)
                if self.deps.verify_attempts >= 3:
                    self.stack.frames.clear()
                    self.stack.queue.clear()
                    self.state.pop('pending_intent', None)
                    self.deps.verify_attempts = 0
                    self.auth_failed = True
                    return 'आपका account verify नहीं हुआ। मैं general policy questions में मदद कर सकता हूँ।'
                return 'इस registered mobile number से कोई account नहीं मिला। कृपया सही registered mobile number बताइए।'
            self.deps.customer = customer
            self.deps.verify_attempts = 0
            self.stack.complete()
            self.event('authentication_completed')
            pending_intent = self.state.pop('pending_intent', None)
            if pending_intent:
                return await self.resolve_customer_intent(
                    pending_intent['action'], pending_intent['entity_query'],
                    source='post_verification',
                )
            return await self.resume()
        gate = self.state.get(GATE_STATE_KEY)
        if gate:
            # This runs before dynamic intent/reference matching. "हाँ cancel
            # कर दो" therefore confirms the armed mutation, never starts one.
            if confirmation_correction(text):
                return None
            verdict, value = await evaluate_gate(gate, text, self.deps)
            if verdict != 'fit' and confirmation_affirmative(text):
                verdict, value = 'fit', True
            if verdict == 'fit':
                self.state.pop(GATE_STATE_KEY, None)
                self.event('confirmation_decision', accepted=value,
                           operation=getattr(self.state.get('pending'), 'op', None))
                await gate.on_fit(value)
                return await self.finish_node()
            return None
        # Do not let "cancel my phone case" fill a status picker. When both
        # intent and a live order reference fit, execute without inference;
        # otherwise the single compiler call handles the wording.
        if flow_id := direct_transaction_flow(normalized):
            return await self.start_direct_transaction(text, flow_id)
        active_slot = await self.fit_active_node_slots(text)
        if active_slot is not None:
            return active_slot
        if transactional_request(normalized):
            return None
        if self.node and self.stack.active:
            for tool in self.node.get('functions', []):
                if tool.name not in SLOT_TO_TOOL:
                    continue
                slot, argument = SLOT_TO_TOOL[tool.name]
                value = self.resolve(normalized, tool, argument, exact=True)
                if value is not None:
                    self.stack.active.slots[slot] = str(value)
                    await self.invoke(tool, {argument: value})
                    return await self.advance_slots()
        return None

    def resolve(self, value, tool, argument, *, exact=False):
        choices = tool.properties[argument].get('enum', [])
        labels = (self.node or {}).get('choices', {}).get(argument, {})
        normalized = norm(str(value))
        if normalized in {'last', 'latest', 'last order', 'latest order'} and argument == 'order_id':
            return (self.node or {}).get('latest_order_id')
        ordinal = {'first': 0, 'पहला': 0, 'second': 1, 'दूसरा': 1, 'third': 2, 'तीसरा': 2}
        index = ordinal.get(normalized)
        if index is not None and index < len(choices):
            return choices[index]
        # IDs often arrive from STT as "AMZ 1004" or spoken digit sequences.
        if argument == 'order_id':
            canonical = canonical_order_id(str(value))
            id_matches = [choice for choice in choices if canonical and
                          canonical_order_id(str(choice)) == canonical]
            if len(id_matches) == 1:
                return id_matches[0]
        # Numeric values are actual enum values first, then displayed ordinals.
        if str(value) in choices:
            return str(value)
        if normalized.isdigit() and 1 <= int(normalized) <= len(choices):
            return choices[int(normalized) - 1]
        matches = [c for c in choices if norm(str(labels.get(c, c))) == normalized]
        if argument == 'variant_id' and not matches:
            aliases = {'extra small': 'xs', 'small': 's', 'medium': 'm',
                       'large': 'l', 'extra large': 'xl', 'double xl': 'xxl'}
            size = aliases.get(normalized, normalized)
            matches = [c for c in choices if size in norm(str(labels.get(c, c))).split()]
        if argument == 'order_id' and not matches:
            query = compact(str(value))
            matches = [c for c in choices if compact(str(labels.get(c, c))) in query]
        if not matches and not exact:
            tokens = set(normalized.split())
            matches = [c for c in choices if tokens and
                       tokens <= set(norm(str(labels.get(c, c))).split())]
        return matches[0] if len(matches) == 1 else None

    async def invoke(self, tool, args):
        # Validate against the live menu again before calling any handler.
        if any(k not in tool.properties for k in args):
            raise ValueError('Unexpected argument')
        for key, prop in tool.properties.items():
            if key in tool.required and key not in args:
                raise ValueError('Missing argument')
            if key in args and 'enum' in prop and args[key] not in prop['enum']:
                raise ValueError('Argument outside live enum')
        self.event('selection', tool=tool.name, arguments=args)
        result, node = await tool.handler(args, self)
        if result and result.get('error'):
            raise ValueError(result['error'])
        if node:
            await self.set_node_from_config(node)

    async def apply(self, batch: CommandBatch, *, sources=None):
        resolution = [command for command in batch.commands if isinstance(command, ResolveIntent)]
        if resolution:
            if len(batch.commands) != 1:
                raise ValueError('Intent resolution cannot share a mutation batch')
            command = resolution[0]
            # At an order picker, a same-flow entity is a target correction
            # ("no, my kurta"), so resolve it against the entire account and
            # run its eligibility check.  At later nodes the same command is
            # more likely a failed slot answer, which must not erase progress.
            if (self.stack.active and command.action == self.stack.active.flow_id and
                    self.current_node.startswith('select_order_') and command.entity_query):
                self.event('active_flow_target_correction',
                           active_flow=self.stack.active.flow_id,
                           entity_query=command.entity_query)
                return await self.resolve_customer_intent(
                    command.action, command.entity_query, source='active_correction'
                )
            # A slot answer that escaped deterministic fitting must not erase
            # the same active frame. A different resolved flow is a genuine
            # new request and is allowed to pre-empt it.
            if self.stack.active and command.action == self.stack.active.flow_id:
                self.event('active_flow_resolution_rejected',
                           active_flow=self.stack.active.flow_id,
                           action=command.action)
                return self.render()
            return await self.resolve_customer_intent(
                command.action, command.entity_query or '', source='compiler'
            )
        if any(isinstance(c, (Clarify, PolicyAnswer, EndCall)) for c in batch.commands):
            if len(batch.commands) != 1:
                raise ValueError('Conversation controls cannot share a mutation batch')
            command = batch.commands[0]
            if isinstance(command, Clarify):
                return CLARIFICATION
            if isinstance(command, PolicyAnswer):
                if not sources or command.source_id not in sources:
                    raise ValueError('Unknown policy source')
                answer = sources[command.source_id]
                if self.node and self.stack.active:
                    answer += ' ' + self.render()
                return answer
            self.ended = True
            self.state.pop('pending', None)
            self.state.pop(GATE_STATE_KEY, None)
            return 'धन्यवाद। आपका दिन अच्छा रहे!'
        self.stack.apply(batch)
        # Stack application is atomic. Once it succeeds, an armed mutation for
        # an old target must not survive to a later confirmation.
        self.invalidate_pending(reason='semantic_frame_change')
        # Re-enter confirmation after ANY semantic turn; no stale consent survives.
        for frame in self.stack.frames:
            if frame.node and frame.node['name'] == 'confirm_mutation':
                frame.dirty = True
        if self.stack.superseded:
            self.event('flow_superseded', frames=[{
                'request_id': frame.request_id, 'flow_id': frame.flow_id,
                'node': (frame.node or {}).get('name'),
            } for frame in self.stack.superseded])
            self.stack.superseded.clear()
        self.event('commands_applied', commands=batch.model_dump()['commands'],
                   frames=self.stack.snapshot())
        if not self.stack.active:
            self.node = None
            return 'ठीक है, कुछ नहीं बदला। और किस तरह मदद कर सकता हूँ?'
        return await self.resume()

    async def resume(self):
        from .nodes.journeys import make_order_summary, make_refund_status_report, make_select_order
        from .nodes.terminal import make_wrap

        frame = self.stack.active
        if frame is None:
            self.node = None
            self.current_node = 'triage'
            return ''
        if self.deps.customer is None:
            if self.auth_failed:
                self.stack.frames.clear()
                self.stack.queue.clear()
                return 'Account help के लिए verification चाहिए। मैं अभी policy questions में मदद कर सकता हूँ।'
            if frame.flow_id != 'verify_phone':
                self.stack.frames.append(FlowFrame('auth_' + uuid4().hex[:8], 'verify_phone'))
            self.node = None
            self.current_node = 'verify_phone'
            return self.phone_prompt()
        self.deps.wip = frame.slots
        if frame.dirty or frame.node is None:
            frame.dirty = False
            refusal = await self.ineligible_order_message(frame)
            if refusal:
                await self.set_node_from_config(await make_wrap(
                    self.deps, self, refusal, name='policy_refusal'
                ))
                return await self.advance_slots()
            if frame.flow_id == 'exchange_item':
                frame.slots.setdefault('resolution', 'exchange')
            node = (await make_refund_status_report(self.deps, self)
                    if frame.flow_id == 'refund_status' else
                    await make_order_summary(self.deps, self)
                    if frame.flow_id == 'order_status' and not frame.slots.get('order_ref') else
                    await make_select_order(self.deps, self, PURPOSES[frame.flow_id]))
            await self.set_node_from_config(node)
        else:
            await self.set_node_from_config(frame.node)
        return await self.advance_slots()

    async def advance_slots(self):
        for _ in range(12):
            if self.current_node in TERMINAL:
                return await self.finish_node()
            moved = False
            for tool in (self.node or {}).get('functions', []):
                if tool.name not in SLOT_TO_TOOL:
                    continue
                key, argument = SLOT_TO_TOOL[tool.name]
                value = self.deps.wip.get(key)
                if value is None:
                    continue
                resolved = self.resolve(value, tool, argument)
                if resolved is None:
                    return 'उस जानकारी से एक available option तय नहीं हुआ। ' + self.render()
                args = {argument: resolved}
                if tool.name == 'select_reschedule_date' and self.deps.wip.get('slot'):
                    args['slot'] = self.deps.wip['slot']
                await self.invoke(tool, args)
                moved = True
                break
            if not moved:
                return self.render()
        raise RuntimeError('Flow did not settle')

    async def finish_node(self):
        speech = self.render()
        if self.current_node in TERMINAL:
            # A policy refusal is a complete response for this turn. Do not
            # auto-promote stale retries and concatenate their response.
            if self.current_node == 'policy_refusal':
                self.stack.complete(promote=False)
                self.stack.discard_stale_queue()
                self.invalidate_pending(reason='policy_refusal')
                self.node = None
                self.current_node = 'triage'
                return speech.strip()
            self.stack.complete()
            self.invalidate_pending(reason='terminal_complete')
            speech += ' ' + await self.resume()
        return speech.strip()

    def render(self):
        speech = (self.node or {}).get('speech')
        if not isinstance(speech, str):
            raise RuntimeError(f'Missing fact renderer for {self.current_node}')
        return speech
