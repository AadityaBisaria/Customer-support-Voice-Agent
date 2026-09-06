# pipecat-bot

Hinglish retail-support voice agent built with Pipecat, Sarvam, Vertex AI
Gemini, a deterministic order store, and a grounded Amazon-style policy corpus.

The core rule is simple: **the model interprets; deterministic code validates,
checks policy, and executes.** The model never receives mutation tools or live
order/item IDs.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the runtime design and safety
boundaries.

## What the assistant can do

After phone verification, the assistant supports:

| Request | Behavior |
| --- | --- |
| Order status | Reads the caller's live orders and delivery facts. |
| Cancellation | Cancels only unshipped orders after an explicit confirmation. |
| Return / refund / replacement | Checks delivery state, item policy, reason, return window, and stock before offering a resolution. |
| Size or color exchange | Offers only eligible apparel variants that are in stock. |
| Delivery reschedule | Offers only active shipments and permitted future dates. |
| Missing-delivery dispute | Opens a dispute only for delivered orders after confirmation. |
| Refund status | Reads existing refund records and expected dates. |
| Policy questions | Answers only from retrieved, verified corpus content; otherwise explicitly states the knowledge limitation. |

The assistant can also explain a mismatch. For example, a dispatched order
cannot be cancelled or returned yet; it can offer tracking and delivery
rescheduling now, and explain what may be available after delivery.

## Demo seed data

Each session receives a newly seeded in-memory store. Use these registered
numbers when asked to verify identity.

| Customer | Registered mobile number | Useful scenarios |
| --- | --- | --- |
| Priya Sharma | `9876543210` | Delivered Anarkali kurta set (`AMZ-1001`) with teal size variants; delivered running shoes; replacement-only Echo Dot; cancellable phone case (`AMZ-1004`). |
| Rahul Verma | `9123456789` | Delivered Cash-on-Delivery mixer; shipped wireless headphones (`AMZ-2002`) that can be rescheduled; cancellable Cash-on-Delivery printed T-shirt (`AMZ-2003`). |
| Anita Desai | `9000000001` | Expired window, already-replaced item, out-of-stock item, non-returnable item, existing refund `REF-2001`, and a missing-delivery dispute. |
| Vikram Singh | `9111111111` | Verified account with no orders. |

Seed dates are relative to the session clock. The source is
`pipecat-bot/server/store/seed.py`.

## Verification behavior

Account-specific requests are suspended until the caller is verified.

- A partial phone number stays inside the verification state; the assistant
  reports the digit count and asks again without calling the LLM.
- A ten-digit number with no matching account receives an account-specific
  re-prompt.
- Three unsuccessful attempts end account-specific help for the session, while
  general grounded policy questions remain available.
- A successful verification resumes the original request and resets the retry
  counter.

## Policy answers

Policy Q&A lives in `server/rag/corpus/qa.json`. Retrieval uses multilingual
embeddings for recall, lightweight lexical reranking across the top candidates,
and a confidence/margin gate. The compiler may cite only a retrieved source.

Examples that are covered by the demo corpus:

- `What is your return policy?`
- `10 30 days returnable ka matlab kya hai?`
- `aapki exchange policy kya hai?`

If no source qualifies, the assistant says it does not have verified
information for that specific policy rather than inventing an answer or using a
generic clarification.

## Configuration

The live LLM is Vertex AI Gemini through Pipecat's
`GoogleVertexLLMService`.

Create `server/.env` from `server/.env.example` and set:

```text
SARVAM_API_KEY=...
VERTEX_PROJECT_ID=...
VERTEX_CREDENTIALS_PATH=C:\path\to\service-account.json
VERTEX_LOCATION=asia-south1
VERTEX_MODEL=gemini-2.5-flash
```

`VERTEX_CREDENTIALS_PATH` must point to a service-account JSON file; do not
commit that file. The supplied `.gitignore` excludes credential JSON files.

## Run locally

From `pipecat-bot/server`:

```powershell
uv sync
uv run bot.py -t webrtc
```

For a headless eval transport:

```powershell
uv run bot.py -t eval
```

On Windows, use UTF-8 for commands that print Devanagari:

```powershell
$env:PYTHONUTF8='1'
```

## Test

From `pipecat-bot/server`:

```powershell
.\.venv\Scripts\python.exe -m pytest flows\tests rag\tests dialogue\tests language\tests -q
```

## Logs and transcripts

Each session creates an append-only JSONL transcript in
`server/logs/transcripts/`. It records system, developer, user, and assistant
turns plus node transitions, slot fits, retrieval/compiler timing, policy
decisions, and mutation confirmation events.

Names follow the weekday-animal convention, for example
`Bear_16_23_05_09_2026.jsonl`; collisions receive `_2`, `_3`, and so on.

## Project layout

```text
pipecat-bot/
  server/
    bot.py                    pipeline assembly and service configuration
    flows/                    compiler protocol, runtime, stack, nodes, confirmation
    dialogue/                 deterministic phone, enum, and yes/no slot fitting
    rag/                      local policy corpus, embeddings, retrieval
    store/                    domain model, policy checks, SQLite adapter, seed data
    transcript.py             JSONL conversation audit log
    evals/                    scripted behavioral scenarios
```
