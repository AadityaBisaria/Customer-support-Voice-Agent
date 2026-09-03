# pipecat-bot

A Hinglish voice agent built with a Pipecat cascade pipeline (STT → LLM → TTS):
an Aryan Retail-style returns/refunds support line that **mirrors the caller's
Hindi/English mix**, answers policy questions **only** from a knowledge base
built from Aryan Retail's public help pages, and — since the transactional
milestone — **does things**: identifies the caller, reports order and refund
status, files returns and exchanges, and cancels unshipped orders against a
mock order store.

The architecture rule throughout: **the LLM proposes, deterministic code
disposes**. Conversation structure is a `pipecat.flows` state machine with ≤4
tools per node and selection enums built from live database IDs; the three
mutations (`cancel_order`, `create_return`, `create_replacement`) are never
LLM tools — they run in code behind a templated readback confirmed by a
deterministic Hinglish yes/no classifier (backchannels like "hmm"/"achha" are
not consent), with policy validators mirroring the KB facts (return windows,
replacement-once, stock checks, per-payment refund timelines) and an
idempotent event-logged SQLite store.

**Demo callers** (browser demo: say the number when asked; Twilio: caller ID
matches automatically): Priya `9876543210`, Rahul `9123456789` (POD/shipped
branches), Anita `9000000001` (every policy refusal), Vikram `9111111111`
(empty account).

## Configuration

- **Bot Type**: Telephony
- **Transport(s)**: Twilio, SmallWebRTC, Daily (WebRTC)
- **Pipeline**: Cascade, plus two custom processors between STT and the
  context aggregator:
  - **STT**: Sarvam `saaras:v3-realtime` in `codemix` mode (built for Hinglish;
    server-side VAD; needs `SARVAM_API_KEY` from https://dashboard.sarvam.ai)
  - **`language/`** — per-turn Hindi-ratio detection (Devanagari codepoints +
    a romanized-Hindi wordlist) → smoothed band (mostly-English / Hinglish /
    mostly-Hindi) → a `[LANG-STYLE]` directive swapped into the LLM context
    only when the caller's band changes
  - **`rag/`** — in-process embedding retrieval over `rag/corpus/qa.json`
    (~55 Q&As from Amazon.in public help pages), run speculatively on interim
    transcripts so the finished turn pays ~0 extra latency; a three-band
    threshold gate (`HIGH` answer-strictly / `MID` answer-if-it-fits /
    `FLOOR` say-you-don't-know) is the anti-fabrication mechanism. Thresholds
    are tuned with `uv run python -m rag.tune_thresholds`.
  - **LLM**: `google/gemma-4-e4b` served locally by [LM Studio](https://lmstudio.ai)
    over its OpenAI-compatible API at `http://127.0.0.1:1234/v1`
  - **TTS**: Sarvam `bulbul:v3` — speaks code-mixed Devanagari+Latin text
    natively, so one voice covers any Hindi/English blend

### Running the LLM locally

The bot talks to LM Studio instead of a hosted provider, so **LM Studio must be
running before you start the bot**:

1. Open LM Studio and load `google/gemma-4-e4b`.
2. Turn on the local server (Developer tab) — it listens on port `1234`.
3. Check it: `curl http://127.0.0.1:1234/v1/models` should list the model.

Point the bot elsewhere by editing `LLM_BASE_URL` / `LLM_MODEL` in `server/.env`.

gemma-4 is a reasoning model. `bot.py` sends `reasoning_effort: "none"` so it
answers immediately instead of spending its first hundred tokens thinking —
which would add seconds of dead air before TTS gets its first word.

## Setup

### Setting Up Twilio

#### 1. Create a TwiML Bin

A TwiML Bin tells Twilio how to handle incoming calls. You'll create one that establishes a WebSocket connection to your bot.

1. Go to the [Twilio Console](https://console.twilio.com)
2. Navigate to **TwiML Bins** → **My TwiML Bins**
3. Click the **+** to create a new TwiML Bin
4. Name your bin and add the TwiML:

    **For Local Development:**

    ```xml
    <?xml version="1.0" encoding="UTF-8"?>
    <Response>
      <Connect>
        <Stream url="wss://your-url.ngrok.io/ws" />
      </Connect>
    </Response>
    ```

    Replace `your-url.ngrok.io` with your ngrok URL.

    **For Pipecat Cloud:**

    ```xml
    <?xml version="1.0" encoding="UTF-8"?>
    <Response>
      <Connect>
        <Stream url="wss://api.pipecat.daily.co/ws/twilio">
          <Parameter name="_pipecatCloudServiceHost"
            value="AGENT_NAME.ORGANIZATION_NAME"/>
        </Stream>
      </Connect>
    </Response>
    ```

    Replace:
    - `AGENT_NAME` with the name of the agent you deployed to Pipecat Cloud
    - `ORGANIZATION_NAME` with the name of your Pipecat Cloud organization

5. Click **Save**

#### 2. Assign TwiML Bin to Your Phone Number

1. Navigate to **Phone Numbers** → **Manage** → **Active Numbers**
2. Click on your Twilio phone number
3. In the "Voice Configuration" section:
   - Set "A call comes in" to **TwiML Bin**
   - Select the TwiML Bin you created
4. Click **Save configuration**

### Server

1. **Navigate to server directory**:

   ```bash
   cd server
   ```

2. **Install dependencies**:

   ```bash
   uv sync
   ```

3. **Configure environment variables**:

   ```bash
   cp .env.example .env
   # Edit .env and add your API keys
   ```

4. **Run the bot**:

   ```bash
   uv run bot.py
   ```

   The runner serves every transport; the caller selects which one (a web/mobile
   client picks its transport when it connects; a telephony provider connects to
   `/ws`).

   For telephony, expose the bot with a public tunnel and point your provider's
   webhook at it:

   ```bash
   ngrok http 7860
   # then set the provider's webhook to wss://<your-ngrok-host>/ws
   ```

## Testing with evals

This project includes behavioral evals: scripted conversations that drive the bot headless — no live call needed. Starter scenarios live in `server/evals/`; edit them as your bot takes shape and copy them to add more.

From `server/`, run the bot with the eval transport, then drive scenarios against it from a second terminal (the bot stays up across runs):

```bash
uv run bot.py -t eval
# In another terminal:
uv run pipecat eval run evals/starter_text.yaml -v    # fast text-mode check
uv run pipecat eval run evals/hinglish_text.yaml -v   # adaptive language mirroring
uv run pipecat eval run evals/rag_grounding.yaml -v   # KB answers + no-fabrication traps
uv run pipecat eval run evals/flows_text.yaml -v      # identify caller + order status
uv run pipecat eval run evals/return_flow.yaml -v     # full return incl. confirm gate
uv run pipecat eval run evals/guards.yaml -v          # failed verification -> KB-only mode
uv run pipecat eval run evals/guards_anita.yaml -v    # policy refusals (window/stock/cancel)
uv run pipecat eval run evals/starter_audio.yaml -v   # full audio round trip (local models, no API keys)
```

Notes for the flow suites: several scenarios end with the bot hanging up
(`end_conversation`), which also ends the bot process — restart `uv run
bot.py -t eval` between suites. The LLM runs with `temperature=0` so scripted
scenarios take the same branch every run. Scenario files contain Devanagari,
so on Windows prefix eval commands with `PYTHONUTF8=1` (PowerShell:
`$env:PYTHONUTF8='1'`).

`eval:` criteria are scored by a judge LLM — a local Ollama by default (`ollama pull gemma2:9b`). The comments in the scenario files cover the schema and how to use an OpenAI judge instead.

## Project Structure

```
pipecat-bot/
├── server/              # Python bot server
│   ├── bot.py           # Main bot implementation + pipeline wiring
│   ├── language/        # Hinglish ratio detection + adaptive [LANG-STYLE] directive
│   ├── rag/             # Zero-latency KB retrieval + anti-fabrication gate
│   │   └── corpus/      # qa.json (Amazon.in help Q&As) + unanswerable.json (traps)
│   ├── store/           # Layered data core: domain (value objects, state machines),
│   │   │                #   policy (KB-mirrored rules), ports (SupportStore Protocol —
│   │   │                #   the production seam), sqlite (event-logged, idempotent), seed
│   ├── flows/           # pipecat.flows graph: nodes, confirm gate, SlotGateProcessor
│   ├── dialogue/        # Deterministic slot toolkit (yes/no consent, phone capture)
│   ├── evals/           # Behavioral eval scenarios (7 suites)
│   ├── pyproject.toml   # Python dependencies
│   ├── .env.example     # Environment variables template
│   ├── .env             # Your API keys (git-ignored)
│   ├── Dockerfile       # Container image for Pipecat Cloud
│   └── pcc-deploy.toml  # Pipecat Cloud deployment config
├── .gitignore           # Git ignore patterns
└── README.md            # This file
```

## Deploying to Pipecat Cloud

This project is configured for deployment to Pipecat Cloud. You can learn how to deploy to Pipecat Cloud in the [Pipecat Quickstart Guide](https://docs.pipecat.ai/getting-started/quickstart#step-2-deploy-to-production).

Refer to the [Pipecat Cloud Documentation](https://docs.pipecat.ai/deployment/pipecat-cloud/introduction) to learn more about configuring, deploying, and managing your agents in Pipecat Cloud.

## Building with an AI coding agent

Extending this bot with Claude Code, Codex, or another AI coding assistant? Give it live, accurate Pipecat context instead of stale training data with the **Pipecat Context Hub** — a local index of Pipecat docs, examples, and API source your agent queries over MCP:

```bash
# Build the local index (first run takes a couple of minutes)
uvx pipecat-ai-context-hub@latest refresh

# Add it to your agent (use the line for the one you use)
claude mcp add pipecat-context-hub -- uvx pipecat-ai-context-hub serve   # Claude Code
codex mcp add pipecat-context-hub -- uvx pipecat-ai-context-hub serve    # Codex
```

MCP servers load at session start, so add it before opening your coding session. See the [Pipecat Context Hub docs](https://docs.pipecat.ai/api-reference/context-hub) for the full setup.

## Learn More

- [Pipecat Documentation](https://docs.pipecat.ai/)
- [Pipecat GitHub](https://github.com/pipecat-ai/pipecat)
- [Pipecat Examples](https://github.com/pipecat-ai/pipecat-examples)
- [Discord Community](https://discord.gg/pipecat)
## Local development on Windows

- **Daily transport is Linux/macOS only.** `daily-python` publishes no Windows
  wheels, so `pyproject.toml` installs it behind a `sys_platform != 'win32'`
  marker and `bot.py` registers the `daily` transport only when the import
  succeeds. It is installed in the Docker/Pipecat Cloud image, so deployed runs
  have Daily; locally use `-t webrtc` (browser) or `-t eval` (headless).
- **Console encoding.** `bot.py` reconfigures stdout/stderr to UTF-8 at import
  so Pipecat's banner and logs don't crash a cp1252 console.
- The `pipecat` CLI itself still prints UTF-8; if a `pipecat` command fails with
  a `charmap` codec error, prefix it with `PYTHONUTF8=1`.
