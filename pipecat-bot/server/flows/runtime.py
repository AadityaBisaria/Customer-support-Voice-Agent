"""Execute validated commands using the existing journey factories and store guards."""

import re
import unicodedata
from uuid import uuid4

from dialogue.phone import PhoneSlot
from dialogue.results import Confidence, Fit
from dialogue.utterance import Utterance
from store.domain import PhoneNumber

from .commands import AbortFlow, Clarify, CommandBatch, EndCall, PolicyAnswer
from .gate_processor import GATE_STATE_KEY, evaluate_gate
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
            'mutation_done', 'mutation_failed', 'mutation_cancelled'}
PHONE_PROMPT = 'कृपया अपना registered mobile number बताइए।'
CLARIFICATION = 'मैं आपकी बात समझना चाहता हूँ। आपको किस order में क्या मदद चाहिए?'


def norm(text):
    # Preserve Devanagari vowel signs/nasalization (Unicode combining marks).
    return ''.join(c for c in text.casefold() if c.isspace() or c == '-' or
                   unicodedata.category(c)[0] in {'L', 'N', 'M'}).strip()


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

    async def shortcut(self, text):
        normalized = norm(text)
        if normalized in {'repeat', 'repeat that', 'phir se', 'दोबारा', 'फिर से बोलिए'}:
            return self.last_speech
        if normalized in {'hmm', 'achha', 'अच्छा'}:
            return ''
        if self.stack.active and self.stack.active.flow_id == 'verify_phone':
            result = await PhoneSlot(name='phone', prompt=PHONE_PROMPT).fit_utterance(
                Utterance.from_text(text), self.deps.fit_ctx())
            if not isinstance(result, Fit) or result.confidence != Confidence.HIGH:
                return None
            customer = await self.deps.store.customer_by_phone(PhoneNumber.parse(result.value))
            if customer is None:
                self.deps.verify_attempts += 1
                if self.deps.verify_attempts >= 2:
                    self.stack.frames.clear()
                    self.stack.queue.clear()
                    self.deps.verify_attempts = 0
                    self.auth_failed = True
                    return 'आपका account verify नहीं हुआ। मैं general policy questions में मदद कर सकता हूँ।'
                return 'इस number से account नहीं मिला। ' + PHONE_PROMPT
            self.deps.customer = customer
            self.deps.verify_attempts = 0
            self.stack.complete()
            self.event('authentication_completed')
            return await self.resume()
        gate = self.state.get(GATE_STATE_KEY)
        if gate:
            # Whole-utterance consent only. Corrections/digressions must be compiled.
            clear = {'yes', 'haan', 'हाँ', 'हां', 'जी हाँ', 'ji haan', 'kar do', 'कर दो',
                     'haan kar do', 'हाँ कर दो', 'go ahead', 'no', 'nahi', 'नहीं', 'rehne do'}
            if normalized not in clear:
                return None
            verdict, value = await evaluate_gate(gate, text, self.deps)
            if verdict == 'fit':
                self.state.pop(GATE_STATE_KEY, None)
                self.event('confirmation_decision', accepted=value,
                           operation=getattr(self.state.get('pending'), 'op', None))
                await gate.on_fit(value)
                return await self.finish_node()
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
        # Numeric values are actual enum values first, then displayed ordinals.
        if str(value) in choices:
            return str(value)
        if normalized.isdigit() and 1 <= int(normalized) <= len(choices):
            return choices[int(normalized) - 1]
        matches = [c for c in choices if norm(str(labels.get(c, c))) == normalized]
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
        self.state.pop(GATE_STATE_KEY, None)
        self.state.pop('pending', None)
        # Re-enter confirmation after ANY semantic turn; no stale consent survives.
        for frame in self.stack.frames:
            if frame.node and frame.node['name'] == 'confirm_mutation':
                frame.dirty = True
        self.event('commands_applied', commands=batch.model_dump()['commands'],
                   frames=self.stack.snapshot())
        if not self.stack.active:
            self.node = None
            return 'ठीक है, कुछ नहीं बदला। और किस तरह मदद कर सकता हूँ?'
        return await self.resume()

    async def resume(self):
        from .nodes.journeys import make_refund_status_report, make_select_order

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
            return PHONE_PROMPT
        self.deps.wip = frame.slots
        if frame.dirty or frame.node is None:
            frame.dirty = False
            if frame.flow_id == 'exchange_item':
                frame.slots.setdefault('resolution', 'exchange')
            node = (await make_refund_status_report(self.deps, self)
                    if frame.flow_id == 'refund_status' else
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
            self.stack.complete()
            self.state.pop('pending', None)
            self.state.pop(GATE_STATE_KEY, None)
            speech += ' ' + await self.resume()
        return speech.strip()

    def render(self):
        speech = (self.node or {}).get('speech')
        if not isinstance(speech, str):
            raise RuntimeError(f'Missing fact renderer for {self.current_node}')
        return speech
