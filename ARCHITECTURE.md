# Architecture

## Design boundary

The system separates language interpretation from authority:

```text
Gemini: interpret one caller turn into a public semantic command
Runtime: resolve live entities, validate state, enforce policy, render facts
Store: execute an idempotent mutation only after deterministic confirmation
```

Gemini has no store tools and cannot provide `order_id`, `item_id`, slots,
eligibility, amounts, dates, or confirmation. Those are derived by code from
the authenticated session and `SupportStore`.

## Per-turn pipeline

```text
Caller speech
  -> Sarvam realtime STT
  -> language-band tracker + JSONL user transcript
  -> CommandRuntime.shortcut
       phone / confirmation / active enum / dynamic order reference / capability follow-up
       -> deterministic fact template -> TTS
  -> local policy retrieval + acceptance gate
       -> grounded answer or explicit policy abstention -> TTS
  -> one Vertex Gemini compiler call, only if still unresolved
       -> strict public command validation
       -> runtime/store pre-actions -> fact template -> TTS
```

There is at most one Gemini call per turn and no separate LLM router. Many
turns resolve without inference: phone digits, confirmations, reasons,
refund/exchange choices, item/order references, sizes, and capability
follow-ups.

`flows/command_processor.py` emits final deterministic speech as Pipecat LLM
response frames, so the normal output/TTS pipeline remains intact without
triggering a second completion.

## Public compiler protocol

`flows/commands.py` defines two schemas.

- `CommandBatch` is the internal protocol used by deterministic runtime code.
  It can start a flow and set validated internal slots.
- `CompilerBatch` is the Vertex-constrained public protocol. It allows only
  `RESOLVE_INTENT`, `FAQ_ANSWER`, `CLARIFY`, and `END_CALL`, with one command
  per model turn.

`RESOLVE_INTENT` carries only a supported action and an untrusted product/order
phrase. `CommandRuntime.resolve_customer_intent()` matches that phrase against
the authenticated caller's full live order history, then checks eligibility
before it creates an internal frame.

## Authentication state machine

Account-specific requests are suspended below a `verify_phone` frame.

```text
Account request -> pending_intent + verify_phone
  -> complete registered number -> customer lookup -> resume pending intent
  -> partial digits -> deterministic length-specific retry
  -> ten digits, unknown account -> deterministic account-specific retry
  -> third unsuccessful attempt -> account help ends; grounded policy remains
```

`PhoneSlot` recognizes digit runs, spoken English/Hindi digits, Devanagari
numerals, `+91`, and a leading `0`. Digit-like invalid input never reaches the
compiler. Successful verification resets the retry count.

## Flow frames and slot fitting

`flows/stack.py` stores `FlowFrame` objects containing a request ID, flow ID,
slots, current node, and dirty state. Frames can be active, suspended, or
explicitly queued.

The runtime applies deterministic handling in this order:

1. Verification and confirmation gates.
2. An active node's declared slot fitter.
3. Explicit, high-confidence personal transaction shortcuts.
4. Dynamic order/item reference fitting built from the caller's own live
   catalogue.
5. One compiler call if no deterministic layer can resolve the turn.

Dynamic product matching normalizes Latin and Devanagari input and conservatively
stems inflections. It is generated from live order titles; it does not hard-code
catalogue products. A correction at `select_order_*` is re-resolved against the
full account history, allowing the system to identify an ineligible target and
explain the mismatch instead of trapping the caller in an eligible-only menu.

### Node-local menus

Journey factories in `flows/nodes/journeys.py` generate constrained menus from
`SupportStore`. The runtime validates every selected value against that menu
before invoking a handler.

Examples:

- `select_order`: live orders permitted for the current step.
- `select_item`: items belonging only to the selected order.
- `select_reason`: return/exchange reasons.
- `select_resolution`: resolutions actually permitted by item policy.
- `select_variant`: in-stock sibling variants.

Enum fitting is semantic but constrained. For example, `refund दे दो` resolves
to `refund`; `refund नहीं, exchange` resolves to `exchange`; neither can create
an option that the node did not expose.

## Eligibility and actionable alternatives

Entity resolution always happens before action eligibility. A real but
ineligible order produces a factual refusal, not “order not found.”

For a dispatched order, the capability context is retained briefly after the
refusal:

```text
Now: track delivery, reschedule delivery
Later: report damage, defect, or wrong item after delivery
Unavailable now: cancellation or refund
```

Thus a follow-up such as “what can I do?” is answered from the exact live order
state without an LLM inference or a repeated product name.

## Confirmation and mutation boundary

```text
Validated live slots
  -> policy/store pre-action
  -> PendingMutation with idempotency key
  -> factual readback
  -> deterministic yes/no gate
  -> flows/confirm.py invokes SupportStore
```

Only `flows/confirm.py` performs transactional store calls. Target changes,
policy refusals, terminal completion, and superseded frames invalidate an armed
pending mutation and its confirmation gate. Store methods repeat eligibility
checks and use idempotency keys, so stale or repeated responses cannot execute
the wrong mutation.

## Grounded policy answers

`rag/corpus/qa.json` contains sourced policy Q&A. `QAIndex` embeds questions
and paraphrases locally. `CommandProcessor.retrieve()`:

1. retrieves the top eight semantic candidates;
2. applies a small lexical rerank over question, paraphrase, and tag tokens;
3. requires a semantic confidence floor and decisive margin, except for an
   exact corpus surface;
4. gives Gemini only the single accepted source ID and answer.

The compiler may emit `FAQ_ANSWER` only for that supplied source. If a
policy-shaped query has no qualifying source, code returns a limited-knowledge
response instead of calling Gemini to invent an answer or emitting a generic
clarification. A policy answer during an active flow preserves and re-renders
the active prompt.

## Flow inventory

| Flow | Authoritative pre-action | Terminal effect |
| --- | --- | --- |
| `order_status` | Read caller orders/delivery | Fact-only status report |
| `cancel_order` | Match all orders, require `PLACED` | Cancel and refund if payment exists |
| `return_order` | Require delivery, policy/window/reason | Create allowed return/refund/replacement |
| `exchange_item` | Require delivery, apparel variant stock | Create exchange |
| `reschedule_delivery` | Require active shipment and valid date | Change delivery date/slot |
| `missing_delivery` | Require delivered order and valid issue | Create investigation |
| `refund_status` | Read refund records | Fact-only refund report |

## Audit log and observability

`transcript.py` writes one JSON object per line to
`server/logs/transcripts/`. It records system/developer/user/assistant turns,
node transitions, slot fits, dynamic entity candidates, eligibility contexts,
retrieval/compiler timings, policy abstentions, confirmation decisions, and
mutation results.

File names use a weekday-animal convention, for example
`Bear_16_23_05_09_2026.jsonl`; collisions append `_2`, `_3`, and so on.

## Project map

```text
server/
  bot.py                       Pipecat pipeline and Vertex/Sarvam wiring
  flows/commands.py            public/internal command schemas and compiler prompt
  flows/command_processor.py   one-call dispatcher, retrieval gate, final frames
  flows/runtime.py             authentication, slots, eligibility, capability context
  flows/stack.py               frame lifecycle and dependent-slot invalidation
  flows/nodes/                 dynamic menus and factual node factories
  flows/confirm.py             only mutation executor
  dialogue/phone.py            spoken-number parser
  rag/                         corpus, embeddings, local index
  store/                       domain, policy, protocol, SQLite seed adapter
  transcript.py                append-only JSONL audit writer
```

## Demo data persistence

`bot.py` opens `server/data/support.db` by default. `SqliteSupportStore`
applies migrations, then seeds the database only if the `customers` table is
empty. New calls therefore receive fresh conversational state but see the
same order, return, refund, delivery, and append-only `domain_events` data.
Set `SUPPORT_STORE_PATH=:memory:` for isolated evaluation-style calls.
