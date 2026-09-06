"""Conversation continuations with isolated slots and an ordered request queue."""

from copy import deepcopy
from dataclasses import dataclass, field

from .commands import AbortFlow, CommandBatch, SetSlot, StartFlow

ALLOWED = {
    'order_status': {'order_ref'}, 'cancel_order': {'order_ref'},
    'return_order': {'order_ref', 'item_ref', 'reason', 'resolution'},
    'exchange_item': {'order_ref', 'item_ref', 'reason', 'resolution', 'variant_ref'},
    'reschedule_delivery': {'order_ref', 'days_ahead', 'slot'},
    'refund_status': set(), 'missing_delivery': {'order_ref', 'dispute_type'},
}
_READ_ONLY = frozenset({'order_status', 'refund_status'})
_TRANSACTIONAL = frozenset({
    'cancel_order', 'return_order', 'exchange_item', 'reschedule_delivery', 'missing_delivery',
})
DEPENDENTS = {
    'order_ref': {'order_id', 'order_item_id', 'item_title', 'item_ref', 'reason',
                  'offered', 'resolution', 'product_id', 'variant_id', 'new_variant_id',
                  'new_variant_label', 'variant_ref', 'days_ahead', 'slot', 'target_date'},
    'item_ref': {'order_item_id', 'item_title', 'reason', 'resolution', 'offered',
                 'product_id', 'variant_id', 'new_variant_id', 'new_variant_label'},
    'reason': {'offered', 'resolution', 'new_variant_id', 'new_variant_label'},
    'resolution': {'new_variant_id', 'new_variant_label'},
}


@dataclass
class FlowFrame:
    request_id: str
    flow_id: str
    slots: dict = field(default_factory=dict)
    node: dict | None = None
    dirty: bool = True
    explicit_sequence: bool = False


@dataclass
class FlowStack:
    frames: list[FlowFrame] = field(default_factory=list)
    queue: list[FlowFrame] = field(default_factory=list)
    superseded: list[FlowFrame] = field(default_factory=list)

    @property
    def active(self):
        return self.frames[-1] if self.frames else None

    def snapshot(self):
        return [{'request_id': f.request_id, 'flow_id': f.flow_id,
                 'step': (f.node or {}).get('name', 'entry'), 'slots': f.slots,
                 'status': 'active' if f is self.active else
                 ('queued' if f in self.queue else 'suspended'),
                 'explicit_sequence': f.explicit_sequence}
                for f in self.frames + self.queue]

    @staticmethod
    def _is_uncommitted(frame: FlowFrame) -> bool:
        name = (frame.node or {}).get('name', '')
        return name.startswith('select_') or name == 'confirm_mutation'

    def discard_stale_queue(self) -> None:
        """Keep only work the caller explicitly asked to do sequentially."""
        self.queue = [frame for frame in self.queue if frame.explicit_sequence]

    def apply(self, batch: CommandBatch):
        # Validate against a copy; no partial state writes if any command fails.
        candidate = deepcopy(self)
        for command in batch.commands:
            all_frames = candidate.frames + candidate.queue
            if isinstance(command, StartFlow):
                if any(f.request_id == command.request_id for f in all_frames):
                    raise ValueError('Duplicate request_id')
                if len(all_frames) >= 8:
                    raise ValueError('Too many outstanding requests')
                frame = FlowFrame(
                    command.request_id, command.flow_id,
                    explicit_sequence=command.mode == 'sequential',
                )
                if command.mode == 'interrupt' and command.flow_id not in {'order_status', 'refund_status'}:
                    raise ValueError('Only read-only flows can interrupt')
                # Never queue a new action behind an unfinished selection prompt.
                # It is an intent correction unless the caller explicitly said
                # "first ... then ..." (which is recorded on queued frames).
                active = candidate.active
                if (active and self._is_uncommitted(active) and
                        command.flow_id in _TRANSACTIONAL):
                    candidate.superseded.append(candidate.frames.pop())
                    candidate.discard_stale_queue()
                    candidate.frames.append(frame)
                elif candidate.active and (command.mode == 'queue' or
                                         any(f.flow_id == 'verify_phone' for f in candidate.frames)):
                    candidate.queue.append(frame)
                else:
                    candidate.frames.append(frame)
            elif isinstance(command, (SetSlot, AbortFlow)):
                frame = next((f for f in all_frames if f.request_id == command.target_request_id), None)
                if frame is None or frame.flow_id == 'verify_phone':
                    raise ValueError('Unknown or protected target')
                if isinstance(command, AbortFlow):
                    (candidate.frames if frame in candidate.frames else candidate.queue).remove(frame)
                else:
                    if command.key not in ALLOWED[frame.flow_id]:
                        raise ValueError('Slot not allowed for this flow')
                    if frame.slots.get(command.key) != command.value:
                        for key in DEPENDENTS.get(command.key, set()):
                            frame.slots.pop(key, None)
                        frame.slots[command.key] = command.value
                        frame.dirty = True
        self.frames, self.queue = candidate.frames, candidate.queue
        self.promote()

    def promote(self):
        if not self.frames and self.queue:
            self.frames.append(self.queue.pop(0))

    def complete(self, *, promote: bool = True):
        if self.frames:
            self.frames.pop()
        if promote:
            self.promote()
