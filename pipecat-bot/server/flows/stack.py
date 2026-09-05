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


@dataclass
class FlowStack:
    frames: list[FlowFrame] = field(default_factory=list)
    queue: list[FlowFrame] = field(default_factory=list)

    @property
    def active(self):
        return self.frames[-1] if self.frames else None

    def snapshot(self):
        return [{'request_id': f.request_id, 'flow_id': f.flow_id,
                 'step': (f.node or {}).get('name', 'entry'), 'slots': f.slots,
                 'status': 'active' if f is self.active else
                 ('queued' if f in self.queue else 'suspended')}
                for f in self.frames + self.queue]

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
                frame = FlowFrame(command.request_id, command.flow_id)
                if command.mode == 'interrupt' and command.flow_id not in {'order_status', 'refund_status'}:
                    raise ValueError('Only read-only flows can interrupt')
                if candidate.active and (command.mode == 'queue' or
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

    def complete(self):
        if self.frames:
            self.frames.pop()
        self.promote()
