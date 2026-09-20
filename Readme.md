# vageesh-back-end

FastAPI backend for Vageesh — an Agentic AI Orchestration Platform.

FastAPI + LangGraph + PostgreSQL, running in a Python virtualenv named `symphony`.

---

## Quick Start — Docker (recommended)

Single command brings up Postgres, runs all migrations, starts the backend and frontend:

```bash
cp .env.example .env          # one-time: fill in ANTHROPIC_API_KEY and optional Slack tokens
docker compose up --build
```

- Frontend: http://localhost
- API docs: http://localhost:8000/docs

Subsequent runs: `docker compose up` (no `--build` needed unless code changes).

---

## Local Dev Setup

### Prerequisites

PostgreSQL must be running locally. Create the database once:

```bash
brew services restart postgresql
psql -U postgres -c "CREATE DATABASE symphony;"
```

### One-time virtualenv setup

```bash
mkvirtualenv symphony
workon symphony
pip install -r requirements.txt
```

### Run migrations

```bash
workon symphony
alembic upgrade head
```

### Start the dev server

```bash
workon symphony
fastapi dev app/main.py
```

- API: http://127.0.0.1:8000
- Interactive docs: http://127.0.0.1:8000/docs

Shut down with `Ctrl+C`.

---

## Tests

139 integration and unit tests covering all routers, the workflow runner, and agent memory.

```bash
# Create a test database (one-time)
psql -U postgres -c "CREATE DATABASE symphony_test;"

# Run all tests
workon symphony
pytest

# Run with coverage
pytest --cov=app --cov-report=term-missing

# Run a single file
pytest tests/integration/test_agents_router.py -v
```

Tests use a dedicated `symphony_test` database. All tables are truncated between tests — the dev database is never touched.

### Test layout

```
tests/
  conftest.py                       ← shared fixtures (test DB, client, auth)
  integration/
    test_agents_router.py
    test_agent_config_router.py
    test_workflows_router.py
    test_workflow_runs_router.py
    test_templates_router.py
    test_messages_router.py
    test_logs_router.py
  unit/
    test_workflow_runner.py
  eval/
    judge.py                          ← shared LLM-as-judge helper
    test_market_signal.py
    test_rm_writer.py
    test_sre_pipeline.py
    test_data_ingestion.py
```

### Evaluate pipelines

Eval tests verify LLM output quality for each built-in template — not just that the API works, but that the agent produces correctly formatted and accurate responses. They run separately from the main suite since they make live LLM calls.

```bash
# Requires ANTHROPIC_API_KEY
pytest -m eval -v
```

Each template is tested for **faithfulness** (does the output stay grounded in the source data — no hallucinated fund names, no invented events) and **relevance** (does the response address what was asked with the right structure and specificity). Checks are a mix of regex assertions for format requirements and a Haiku-powered LLM judge for semantic quality. No new dependencies are added — the judge reuses `langchain-anthropic` already in the stack.

---

## Source Layout

```
app/
  main.py              ← FastAPI app, router registration, lifespan (Slack + scheduler)
  database.py          ← async SQLAlchemy engine and session factory
  dependencies.py      ← shared FastAPI dependencies (DB session, auth)
  workflow_runner.py   ← LangGraph execution engine (MAX_LOOPS = 20)
  scheduler.py         ← APScheduler integration for cron workflows
  slack_bot.py         ← Socket Mode Slack bot (starts/stops with FastAPI)
  ws_manager.py        ← WebSocket connection manager (broadcast run events)
  routers/             ← route handlers grouped by domain
  schemas/             ← Pydantic request/response models
  models/              ← SQLAlchemy ORM models
  templates/           ← built-in workflow templates
alembic/               ← database migrations
  versions/            ← migration scripts (0001 → 0007)
alembic.ini
requirements.txt
Dockerfile
docker-compose.yml
.env.example
```

---

## Database Management

### Alembic Migrations

```bash
workon symphony

# Apply all pending migrations
alembic upgrade head

# Create a new migration after changing SQLAlchemy models
alembic revision --autogenerate -m "describe_change"

# Check current state
alembic current
alembic history
```

### Schema Overview

| Table | Key columns |
|---|---|
| `agents` | id, name, model, description, system_prompt, tools (JSON), channels (JSON), skills (JSON), interaction_rules (JSON), guardrails (JSON), memory_enabled, status |
| `agent_schedules` | id, agent_id (FK), cron_expr, enabled, created_at |
| `agent_memory` | id, agent_id (FK), key, value, created_at, updated_at |
| `workflows` | id, name, description, graph_definition (JSON), status, schedule (cron), trigger_type |
| `workflow_runs` | id, workflow_id (FK), status, input (JSON), output (JSON), usage (JSON), started_at, finished_at |
| `messages` | id, agent_id (FK), session_id, role, content, created_at |
| `logs` | id, agent_id (FK), workflow_id (FK), level, message, metadata (JSON), created_at |

---

## API Overview

All endpoints are under `/api/v1` and require `Authorization: Bearer <token>`.
Auth is currently a stub — any non-empty token is accepted.

| Resource | Endpoints |
|---|---|
| Agents | GET/POST `/agents`, GET/PUT/DELETE `/agents/{id}` |
| Agent Memory | GET/POST `/agents/{id}/memory`, GET/DELETE `/agents/{id}/memory/{key}` |
| Agent Schedules | GET/POST `/agents/{id}/schedules`, PUT/DELETE `/agents/{id}/schedules/{schedule_id}` |
| Agent Skills | PUT `/agents/{id}/skills` |
| Interaction Rules | PUT `/agents/{id}/interaction-rules` |
| Guardrails | PUT `/agents/{id}/guardrails` |
| Workflows | GET/POST `/workflows`, GET/PUT/DELETE `/workflows/{id}` |
| Workflow Runs | POST `/workflows/{id}/run`, GET `/workflows/{id}/runs`, GET `/workflows/{id}/runs/{run_id}` |
| Templates | GET `/templates`, POST `/templates/{id}/instantiate` |
| Messages | GET/POST `/messages`, GET/DELETE `/messages/{id}` |
| Logs | GET/POST `/logs`, GET `/logs/{id}` |

### WebSocket

| Resource | URL |
|---|---|
| Run event stream | `ws://localhost:8000/ws/workflows/{workflow_id}/runs/{run_id}?token=<token>` |

Events: `node_enter`, `node_complete`, `edge_traverse`, `run_complete`, `run_error`

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://postgres:postgres@localhost/symphony` | PostgreSQL async connection string |
| `ANTHROPIC_API_KEY` | — | **Required.** Anthropic API key for Claude models |
| `SLACK_BOT_TOKEN` | — | Slack bot token (`xoxb-...`) for Socket Mode |
| `SLACK_APP_TOKEN` | — | Slack app-level token (`xapp-...`) for Socket Mode |
| `SLACK_REPORT_CHANNEL` | `data-reports` | Slack channel for pipeline reports |
| `LANGCHAIN_TRACING_V2` | `false` | Set to `true` to enable LangSmith tracing |
| `LANGCHAIN_API_KEY` | — | LangSmith API key (get at smith.langchain.com) |
| `LANGCHAIN_PROJECT` | `symphony` | LangSmith project name |
| `DATASET_DIR` | — | Path to dataset directory for the Data Ingestion Pipeline template |
| `POSTGRES_PASSWORD` | `postgres` | Used by docker-compose only |

Copy `.env.example` → `.env` and fill in your values.

---

## Slack Integration

Vageesh includes a Socket Mode Slack bot (`app/slack_bot.py`) that starts automatically with the FastAPI server.

### How it works

- Listens for **direct messages** (`message.im`) and **@mentions** (`app_mention`)
- Routes each message to the first agent whose `channels` list includes `"slack"`
- The agent's model and system prompt are used to generate a reply via Claude (LangGraph)
- Both the inbound message and reply are persisted to the `messages` table; the Slack channel ID is used as `session_id`
- Falls back to `claude-haiku-4-5-20251001` with a generic prompt if no agent is configured for Slack
- Gracefully disables itself if `SLACK_BOT_TOKEN` or `SLACK_APP_TOKEN` are not set

### Setup

1. Create a Slack app at [api.slack.com/apps](https://api.slack.com/apps) with **Socket Mode** enabled
2. Add bot scopes: `chat:write`, `im:history`, `app_mentions:read`
3. Subscribe to events: `message.im`, `app_mention`
4. Set tokens in `.env`:
   ```
   SLACK_BOT_TOKEN=xoxb-...
   SLACK_APP_TOKEN=xapp-...
   ```
5. In the Vageesh UI (**Agent Configuration → Channels**), add `slack` to the agent that should handle Slack messages

---

## Adding a New Messaging Channel

Vageesh's channel system is designed to make adding a new integration (Telegram, WhatsApp, Discord, etc.) straightforward. All channels follow the same pattern as the Slack bot.

### How channels work

An agent's `channels` field is a JSON list of strings (e.g. `["slack", "telegram"]`). When a message arrives on a channel, the bot queries the database for the first agent whose `channels` list contains that channel name, then routes the message to it.

### Step 1 — Create `app/<channel>_bot.py`

Model it on `app/slack_bot.py`. The key sections to implement:

```python
# 1. Agent lookup — identical pattern for every channel
async def _get_<channel>_agent() -> Agent | None:
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(Agent))).scalars().all()
        for agent in rows:
            if any(c.lower() == "<channel>" for c in (agent.channels or [])):
                return agent
    return None

# 2. Message persistence — call this for every inbound and outbound message
#    session_id groups messages into a conversation (use channel/chat ID as seed)
await _save_message(role, content, agent_id, session_id)

# 3. LLM call — use the shared helper or copy _run_agent_direct()
#    It reads agent.model and agent.system_prompt automatically

# 4. Lifecycle functions — called by main.py lifespan
async def start_<channel>_bot() -> None: ...
async def stop_<channel>_bot() -> None: ...
```

`_save_message` is a copy-paste from `slack_bot.py` — it writes to the `messages` table with the agent ID and a UUID-derived session ID.

### Step 2 — Wire into `app/main.py`

```python
from app.<channel>_bot import start_<channel>_bot, stop_<channel>_bot

@asynccontextmanager
async def lifespan(app: FastAPI):
    await start_slack_bot()
    await start_<channel>_bot()   # add here
    await start_scheduler()
    yield
    await stop_scheduler()
    await stop_<channel>_bot()    # and here
    await stop_slack_bot()
```

### Step 3 — Add env vars

Add any required tokens/keys to `.env.example`:

```
<CHANNEL>_BOT_TOKEN=...
```

And read them in your bot file:

```python
token = os.environ.get("<CHANNEL>_BOT_TOKEN", "")
if not token:
    logger.warning("<Channel> bot disabled — <CHANNEL>_BOT_TOKEN not set.")
    return
```

### Step 4 — Configure an agent

In the Vageesh UI (**Agent Configuration → Channels**), add the channel name string (e.g. `telegram`) to the agent that should handle messages from that channel. The bot picks it up immediately on the next message.

### Channel routing summary

```
Inbound message
    ↓
Bot queries DB: SELECT * FROM agents WHERE channels @> '["<channel>"]'
    ↓
Found?  → use agent.model + agent.system_prompt
Not found? → fall back to claude-haiku-4-5 with generic prompt
    ↓
Check for message-triggered workflow containing this agent
    ↓
Workflow found? → run_workflow()  |  Not found? → direct LLM call
    ↓
Persist user + assistant messages to messages table
    ↓
Send reply back through channel
```

---

## Observability — LangSmith Tracing

Vageesh integrates with [LangSmith](https://smith.langchain.com) for deep LLM tracing. When enabled, every workflow run and Slack bot message is automatically traced — including full prompt/response content, token counts, cost, and per-node latency.

### Setup

1. Sign up at [smith.langchain.com](https://smith.langchain.com) and create a project named `symphony`
2. Go to **Settings → API Keys** and generate a new key
3. Add to `.env`:
   ```
   LANGCHAIN_TRACING_V2=true
   LANGCHAIN_API_KEY=lsv2_pt_...
   LANGCHAIN_PROJECT=symphony
   ```
4. Restart the backend — tracing starts immediately on the next workflow run or Slack message

### What gets traced

| Trigger | What you see in LangSmith |
|---|---|
| Workflow run (Agent node) | Full LangGraph run tree, per-node latency, Claude prompt + response, token usage, cost |
| Slack message | Single LLM call with system prompt, user message, and reply |
