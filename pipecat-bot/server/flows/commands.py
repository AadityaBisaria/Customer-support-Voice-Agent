"""Validated semantic output. Commands describe requests, never grant consent."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

FlowId = Literal['order_status', 'cancel_order', 'return_order', 'exchange_item',
                 'reschedule_delivery', 'refund_status', 'missing_delivery']


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class StartFlow(StrictModel):
    type: Literal['START_FLOW']
    request_id: str = Field(min_length=1, max_length=64)
    flow_id: FlowId
    mode: Literal['queue', 'interrupt'] = 'queue'


class SetSlot(StrictModel):
    type: Literal['SET_SLOT']
    target_request_id: str
    key: Literal['order_ref', 'item_ref', 'reason', 'resolution', 'variant_ref',
                 'days_ahead', 'slot', 'dispute_type']
    value: str = Field(min_length=1, max_length=200)


class AbortFlow(StrictModel):
    type: Literal['ABORT_FLOW']
    target_request_id: str


class Clarify(StrictModel):
    type: Literal['CLARIFY']


class PolicyAnswer(StrictModel):
    type: Literal['FAQ_ANSWER']
    source_id: str


class EndCall(StrictModel):
    type: Literal['END_CALL']


Command = Annotated[StartFlow | SetSlot | AbortFlow | Clarify | PolicyAnswer | EndCall,
                    Field(discriminator='type')]


class CommandBatch(StrictModel):
    commands: list[Command] = Field(min_length=1, max_length=12)
    bridge_text: str | None = Field(default=None, max_length=200)


COMPILER_PROMPT = '''You interpret retail support turns into one JSON command batch.
Output ONLY JSON matching the supplied schema. No markdown. No tool calls.
You are a male assistant: any Hindi first-person wording is masculine and in Devanagari.
START_FLOW requests a supported business goal. Give each new request a unique request_id.
SET_SLOT always targets its specific request_id, including queued requests.
Extract flow AND all explicitly stated arguments together. Preserve informal references:
order_ref="shoes", item_ref="kurta". Never invent database IDs, account facts or policy.
Supported flows: order_status (list/history/latest/status/tracking), cancel_order
(cancel an unshipped ORDER only), return_order (refund/replacement of delivered items),
exchange_item (size/color exchange), reschedule_delivery, refund_status (existing refunds),
missing_delivery (delivered but missing/empty/wrong location).
Withdrawing a RETURN REQUEST is unsupported: CLARIFY; do not start cancel_order.
For a correction, SET_SLOT on the existing frame, not a duplicate START_FLOW.
For a temporary question use mode=interrupt; for multiple sequential requests use queue.
ABORT_FLOW only for an explicit abandonment, never infer it merely from a digression.
When waiting for phone verification, do not extract phone digits into a business slot.
Never emit confirmation/consent: deterministic code alone handles yes/no.
reason: damaged, defective, wrong_item, missing_parts, not_needed, size_issue.
resolution: refund, replacement, exchange. days_ahead is 1, 2 or 3.
slot: morning, afternoon, evening. dispute_type: item_not_received, empty_box, wrong_location.
FAQ_ANSWER chooses a supplied grounded source_id only when it actually answers the question.
CLARIFY for unknown/ambiguous/unsupported requests. END_CALL only for an explicit goodbye.
bridge_text is optional, short, non-factual acknowledgement; never promise success,
refunds, timelines or imply verification. Prefer null when no lookup is needed.
Use only one of these approved bridges or null: "जी, मैं देख रहा हूँ।",
"ज़रूर, मैं check कर रहा हूँ।", "Sure, I'll check that for you."
'''
