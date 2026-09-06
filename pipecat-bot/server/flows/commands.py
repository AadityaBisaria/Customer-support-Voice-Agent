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
    mode: Literal['queue', 'sequential', 'interrupt'] = 'queue'


class ResolveIntent(StrictModel):
    """An untrusted request; only the backend may bind live order/item IDs."""

    type: Literal['RESOLVE_INTENT']
    request_id: str = Field(min_length=1, max_length=64)
    action: FlowId
    entity_query: str | None = Field(default=None, min_length=1, max_length=200)


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


Command = Annotated[ResolveIntent | StartFlow | SetSlot | AbortFlow | Clarify | PolicyAnswer | EndCall,
                    Field(discriminator='type')]


class CommandBatch(StrictModel):
    commands: list[Command] = Field(min_length=1, max_length=12)
    bridge_text: str | None = Field(default=None, max_length=200)


# The LLM only receives this public protocol. Internal stack commands are
# deliberately excluded, so a hallucinated item_ref cannot reach a mutation.
CompilerCommand = Annotated[ResolveIntent | Clarify | PolicyAnswer | EndCall,
                            Field(discriminator='type')]


class CompilerBatch(StrictModel):
    commands: list[CompilerCommand] = Field(min_length=1, max_length=1)
    bridge_text: str | None = Field(default=None, max_length=200)


def vertex_compiler_schema() -> dict:
    """Small Vertex-safe JSON Schema for the public compiler protocol.

    This deliberately mirrors ``CompilerBatch`` without Pydantic's titles,
    discriminator metadata, or backend-only command definitions. Vertex applies
    it while decoding; Pydantic remains the post-generation trust boundary.
    """
    command = lambda properties, required: {
        'type': 'object', 'additionalProperties': False,
        'properties': properties, 'required': required,
    }
    type_field = lambda value: {'type': 'string', 'enum': [value]}
    return {
        'type': 'object', 'additionalProperties': False,
        'properties': {
            'commands': {
                'type': 'array', 'minItems': 1, 'maxItems': 1,
                'items': {'oneOf': [
                    command({
                        'type': type_field('RESOLVE_INTENT'),
                        'request_id': {'type': 'string'},
                        'action': {'type': 'string', 'enum': list(FlowId.__args__)},
                        'entity_query': {'anyOf': [{'type': 'string'}, {'type': 'null'}]},
                    }, ['type', 'request_id', 'action']),
                    command({'type': type_field('FAQ_ANSWER'),
                             'source_id': {'type': 'string'}}, ['type', 'source_id']),
                    command({'type': type_field('CLARIFY')}, ['type']),
                    command({'type': type_field('END_CALL')}, ['type']),
                ]},
            },
            'bridge_text': {'anyOf': [{'type': 'string'}, {'type': 'null'}]},
        },
        'required': ['commands'],
    }


COMPILER_PROMPT = '''You interpret retail support turns into one JSON command batch.
Output ONLY JSON matching the supplied schema. No markdown. No tool calls.
You are a male assistant: any Hindi first-person wording is masculine and in Devanagari.
For every customer-specific order request, emit RESOLVE_INTENT, never START_FLOW or SET_SLOT.
RESOLVE_INTENT carries a supported action and the caller's exact product/order phrase as entity_query.
Example: "Cancel my kurta" -> action="cancel_order", entity_query="kurta".
For a request without an item, entity_query is null. Never emit order_ref, item_ref, an order ID,
or a mutation payload: live entity resolution and eligibility are backend-only.
START_FLOW and SET_SLOT are reserved for backend compatibility and must not be emitted.
Supported flows: order_status (list/history/latest/status/tracking), cancel_order
(cancel an unshipped ORDER only), return_order (refund/replacement of delivered items),
exchange_item (size/color exchange), reschedule_delivery, refund_status (existing refunds),
missing_delivery (delivered but missing/empty/wrong location).
Withdrawing a RETURN REQUEST is unsupported: CLARIFY; do not start cancel_order.
For a correction, emit a new RESOLVE_INTENT with the corrected entity_query.
When waiting for phone verification, do not extract phone digits into a business slot.
Never emit confirmation/consent: deterministic code alone handles yes/no.
FAQ_ANSWER chooses a supplied grounded source_id only when it actually answers the question.
CLARIFY for unknown/ambiguous/unsupported requests. END_CALL only for an explicit goodbye.
bridge_text is optional, short, non-factual acknowledgement; never promise success,
refunds, timelines or imply verification. Prefer null when no lookup is needed.
Use only one of these approved bridges or null: "जी, मैं देख रहा हूँ।",
"ज़रूर, मैं check कर रहा हूँ।", "Sure, I'll check that for you."
'''
