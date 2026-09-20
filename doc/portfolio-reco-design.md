# Portfolio Recommendation Agent — Design Document

**Feature:** Market Signal → Portfolio Recommendation
**Platform:** Vageesh (Agentic AI Orchestration Platform)
**Deliverable:** Workflow template running end-to-end against real market events

---

## Problem Statement

A market event occurs (e.g. RBI rate decision, oil price shock, geopolitical event). The agent must:
1. Identify which instruments/sectors it touches — directly and via second-order exposure
2. Match affected sectors against each investor's actual holdings to determine who is impacted and by how much
3. Generate a recommendation per affected investor, constrained to the product universe
4. Output a message an RM could read in one glance

**Hard constraints:**
- Portfolio, CIO outlook, and product_universe data must be treated as pre-ingested
- Never recommend an instrument outside the given product_universe
- RM-facing output only

---

## Pipeline

```
Start
  │
  ▼
[Agent]         Market Signal Watcher
  │
  ▼
[Pipeline Tool] Portfolio Impact Analyzer
  │
  ▼
[Pipeline Tool] Product Universe Filter
  │
  ▼
[Agent]         RM Recommendation Writer
  │
  ▼
End
```

Built as a workflow template in Vageesh, following the same pattern as `data_ingestion.py` and `sre_report.py`.

---

## Node-by-Node Design Decisions

---

### Node 1 — Market Signal Watcher
**Type:** Agent (LLM + Tool)
**Model:** claude-haiku-4-5
**Tool:** `fetch_rss_signal` (TOOL_REGISTRY)

Fetches real financial news headlines from RSS feeds (ET Markets, Moneycontrol) and reasons about which sectors are directly and indirectly affected.

**Why Agent, not Pipeline Tool:** The task is causal reasoning, not data transformation. Identifying that an oil price spike touches energy funds directly but also raises input costs for Industrials, FMCG, and Transportation via second-order effects — requires the LLM's understanding of economic relationships, not a lookup.

**Why RSS, not yfinance:** Price data (yfinance, NSE indices) shows what already happened. RSS gives the event narrative driving second-order reasoning. `yfinance` tells you Nifty Bank dropped 2%; it doesn't tell you why or which portfolio positions are exposed. The signal source is event-first, not price-first.

**State output:** `EVENT_SUMMARY`, `ALL_AFFECTED_SECTORS`, `SENTIMENT`

---

### Node 2 — Portfolio Impact Analyzer
**Type:** Pipeline Tool (no LLM)
**Registered in:** PIPELINE_TOOLS

Loads `sample_portfolios.csv` and `product_catalogue.csv` from `DATASET_DIR`. For each investor, computes weighted sector exposure against the affected sectors, flags investors above the 5% materiality threshold, and drafts top-3 alternative products from the universe.

**Why Pipeline Tool, not Agent:** Pure deterministic computation — load CSVs, string-match sectors, compute weighted arithmetic, filter by risk profile. An LLM would add latency, cost, and non-determinism to a task with a single correct answer.

**Impact threshold:** 5% weighted sector exposure. Below this, the noise-to-signal ratio is too high for a meaningful RM action. This threshold is a reasonable starting point but should be empirically tuned — see Design Decisions below.

**Alternatives:** Top 3 products from the universe, filtered by: not already held by the investor, and appropriate sub-category for the investor's risk profile + market sentiment direction.

**State output:** `portfolio_impact` — structured per-investor report with affected holdings, weighted exposure, ₹ at risk, and draft alternatives

---

### Node 3 — Product Universe Filter
**Type:** Pipeline Tool (no LLM)
**Registered in:** PIPELINE_TOOLS

Cross-checks every recommended fund ID in `portfolio_impact` against `product_catalogue.csv`. Strips any fund not in the universe. Flags investors where stripping leaves zero valid alternatives.

**Why this node exists:** The hard constraint cannot be delegated to the LLM alone. A system prompt can instruct the model to stay within the universe, but models can blend training knowledge with provided data or reference funds not in the alternatives list. A deterministic filter after the LLM guarantees compliance.

**Matching strategy:** Match on `fund_id`, not `fund_name`. Names have spacing and abbreviation variations; IDs are exact.

**When zero alternatives remain:** The investor is not silently dropped. They appear in the Slack output with a `⚠️ NO SUITABLE PRODUCT IN UNIVERSE — RM MUST REVIEW MANUALLY` flag. Silent exclusion removes the compliance trail; the RM needs to know the gap exists.

**Audit trail:** The filter logs every stripped fund ID — required for explainability in wealth management.

**State output:** `validated_impact` — same structure as `portfolio_impact` with non-universe funds removed and ⚠️ flags added

---

### Node 4 — RM Recommendation Writer
**Type:** Agent (LLM + Tool)
**Model:** claude-haiku-4-5
**Tool:** `publish_rm_alert` (TOOL_REGISTRY)

Reads `validated_impact` from state, writes per-investor action notes with second-order reasoning, and calls `publish_rm_alert` to post a consolidated Slack message to `#portfolio-reco`.

**Why Agent, not Pipeline Tool:** Writing advisory copy — connecting a specific market event to a specific investor's holdings, including second-order exposure reasoning — requires language generation. The LLM reads only from `validated_impact`; it cannot add funds from its own training knowledge.

**Output format:** One consolidated Slack message, grouped by investor, ≤80 words per block. ⚠️ flag where no universe product was available.

---

## Tool Registry Summary

| Tool | Type | Used By | Purpose |
|---|---|---|---|
| `fetch_rss_signal` | TOOL_REGISTRY (`@tool`) | Market Signal Watcher | Fetch and parse RSS financial headlines |
| `portfolio_impact_analyzer` | PIPELINE_TOOLS | Portfolio Impact Analyzer | Load CSVs, score exposure, draft alternatives |
| `product_universe_filter` | PIPELINE_TOOLS | Product Universe Filter | Strip non-universe funds, add ⚠️ flags |
| `publish_rm_alert` | TOOL_REGISTRY (`@tool`) | RM Recommendation Writer | Post consolidated Slack alert to #portfolio-reco |

---

## Telemetry — LangSmith

```
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=<key>
LANGCHAIN_PROJECT=portfolio
```

All LangGraph calls are automatically traced. Each workflow run is tagged with `run_name`, `workflow_id`, and `run_id` in the `ainvoke` config for per-run visibility in the LangSmith dashboard.

---

## Configuration

| Env Var | Purpose |
|---|---|
| `DATASET_DIR` | Path to pre-ingested CSVs |
| `SLACK_BOT_TOKEN` | Slack bot authentication |
| `SLACK_REPORT_CHANNEL` | Target channel (`portfolio-reco`) |
| `LANGCHAIN_TRACING_V2` | Enable LangSmith tracing |
| `LANGCHAIN_API_KEY` | LangSmith API key |
| `LANGCHAIN_PROJECT` | LangSmith project name |
| `ANTHROPIC_API_KEY` | Claude model access |

---

## Data Files (pre-ingested)

| File | Records | Purpose |
|---|---|---|
| `sample_portfolios.csv` | 15 investors, 89 holdings | Investor portfolios with fund weights |
| `product_catalogue.csv` | 33 mutual funds | The only instruments that can be recommended |

---

## Vageesh Platform — Industry Best Practices

### Agent Architecture

| Practice | Implementation |
|---|---|
| Separation of concern | Each node has exactly one responsibility; Market Signal Watcher is decoupled from portfolio scoring and recommendation writing |
| Inter-agent communication | LangGraph StateGraph passes typed state between nodes; agent-to-agent handoffs feature enables cross-workflow delegation |
| Dedicated vs shared capabilities | PIPELINE_TOOLS (deterministic, no LLM) are kept separate from TOOL_REGISTRY tools (LLM-invoked via `create_react_agent`) |
| Real-time channel integration | Slack Socket Mode bot (inbound) + `publish_rm_alert` (outbound) — bidirectional |
| Containerized deployment | `docker compose up --build` brings up PostgreSQL, runs Alembic migrations, starts backend and frontend |

### Platform Best Practices

| Practice | Implementation |
|---|---|
| Codebase separation | Three repositories: `vageesh-prgm-mgmt` (planning), `vageesh-front-end` (UI), `vageesh-back-end` (platform) |
| Centralized agent definition | Agents are PostgreSQL records managed via the Vageesh UI — not hardcoded per project. One agent definition is reusable across all workflows that need it |
| Agent communication | Async Python (`asyncio`) within workflow runs; run events streamed to clients via WebSocket (`node_enter`, `node_complete`, `edge_traverse`). Note: this is async I/O, not a distributed message queue — message-queue async (Kafka, Redis Streams) would be the production evolution |
| Message history | Every agent message written to the `messages` table with `session_id`; workflow outputs stored in `workflow_runs`; surfaced in the Vageesh UI |
| Live monitoring | WebSocket run event stream, LangSmith tracing (full prompt/response, token counts, cost per node, latency), persistent logs via `logs` router |

### Separation of Concerns

| Practice | Implementation |
|---|---|
| Orchestration vs implementation | `workflow_runner.py` + LangGraph is the execution engine; business logic lives in tools and templates — they do not touch each other |
| Agent definition vs codebase | Agents are database records — system prompt, model, tools, channels all editable via UI without a code deploy |
| Reuse and proliferation control | Centralized agent registry; one agent definition serves multiple workflows |
| Persistence | Agents, workflows, runs, memory, messages, and logs all in PostgreSQL with Alembic-managed migrations |
| Auth | Scaffolded with a single `get_current_user` FastAPI dependency — the correct pattern for centralized auth. Currently accepts any non-empty token. Production hardening (JWT, RBAC, rate limiting) applies at this single point without touching business logic |

---

## What We'd Do Differently With More Time

- **CIO outlook integration:** The hard constraint lists CIO outlook as pre-ingested data but the sample dataset does not include it. With it, the recommendation logic could factor in house views on top of the market event signal.
- **Multiple RSS sources with deduplication:** Currently two feeds. Additional sources with deduplication would reduce the risk of missing a significant event.
- **Investor risk profile validation:** Currently the `risk_profile` field is used as-is. A more robust system would validate that recommended alternatives don't breach the investor's KYC-declared risk tolerance.
- **Scheduled trigger:** Run automatically at market close (3:30 PM IST) rather than manual trigger.
- **Feedback loop:** RM acknowledgement via Slack reaction logged back to the system for recommendation quality tracking.

---

## Design Decision We're Least Sure About

**The 5% weighted sector exposure threshold for "material impact."**

Too high: investors with meaningful but sub-5% exposure to a volatile event are missed.
Too low: the RM receives noise — every market move affects every portfolio slightly.

5% is a reasonable starting point but should be empirically tuned against historical events and RM feedback. Ideally it would be a configurable parameter per workflow run, not hardcoded.
