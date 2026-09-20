# Portfolio Recommendation Agent

> Portfolio Mgmt deliverable — built as a workflow template on Vageesh, an Agentic AI Orchestration Platform.

---

## Overview

A four-node LangGraph pipeline that fetches live Indian market headlines, scores each investor's weighted sector exposure against the affected sectors, enforces the product universe constraint deterministically, and posts a consolidated RM-ready alert to `#portfolio-reco` on Slack.

**Trigger:** Manual, from the Vageesh UI
**Data:** Pre-ingested CSVs — 15 investors, 89 holdings, 33 approved funds
**Telemetry:** LangSmith (token counts, cost, latency per node)

For architecture decisions, design trade-offs, and industry best practices: [`doc/portfolio-reco-design.md`](doc/portfolio-reco-design.md)

---

## Pipeline

```
[Agent]         Market Signal Watcher       RSS headlines → LLM identifies event + direct/second-order sector impacts
      ↓
[Pipeline Tool] Portfolio Impact Analyzer   Scores weighted exposure per investor, drafts top-3 alternatives
      ↓
[Pipeline Tool] Product Universe Filter     Strips non-universe fund IDs, flags investors with zero valid alternatives (⚠️)
      ↓
[Agent]         RM Recommendation Writer    Writes per-investor action notes, posts consolidated alert to #portfolio-reco
```

---

## How to Run

1. **Set environment variables** in `.env`:
   ```
   DATASET_DIR=/Users/ajay/tech/dataset/portfolio
   SLACK_BOT_TOKEN=xoxb-...
   SLACK_REPORT_CHANNEL=portfolio-reco
   LANGCHAIN_TRACING_V2=true
   LANGCHAIN_API_KEY=lsv2_pt_...
   LANGCHAIN_PROJECT=portfolio
   ```

2. **Create `#portfolio-reco`** in Slack and invite the bot.

3. **Start the server** on the `portfolio-reco` branch:
   ```bash
   workon symphony
   fastapi dev app/main.py
   ```

4. **Run from the UI** — Workflows → Portfolio Recommendation → Run.

> Each workflow instantiation freezes the graph from the template. To pick up template changes, delete the existing instance and create a new one.

---

## Sample Output — Event 1 (Oil / Geopolitical Risk)

```
🔔 Market Alert: Elevated oil prices amid Middle East geopolitical tensions driving up
Eurozone bond yields and global risk premiums; JPMorgan CEO warns of systemic risks

**Priya Mehta (INV001)** | Moderate, age 42, Mid-career with two school-age children
₹591,240 at risk in HDFC Index Fund – Sensex Plan & HDFC Flexi Cap Fund (49.3% exposure).
Rising oil prices and Eurozone yield hikes pressure equity valuations; energy/banking sector
overlap in her large-cap holdings amplifies losses. Redirect to defensive large-caps.
→ Consider: ICICI Prudential Bluechip Fund, Mirae Asset Large Cap Fund, Nippon India Large Cap Fund

**Vikram Sharma (INV002)** | Aggressive, age 35, Early-career, recently married
₹1,536,798 at risk across 6 holdings (51.2% exposure).
Geopolitical risk and systemic warnings trigger broad selloff in mid/small caps he holds.
Elevated bond yields make growth multiples untenable. Consolidate into stable large-cap core.
→ Consider: ICICI Prudential Bluechip Fund, Mirae Asset Large Cap Fund, Nippon India Large Cap Fund

**Karan Bedi (INV015)** | Aggressive, age 36, Mid-career
₹1,283,978 at risk in ICICI Large & Mid Cap, Bandhan Small Cap, Bandhan Large & Mid Cap (58.4% exposure).
Small and mid-cap concentration amplifies vulnerability to geopolitical and AI-spending uncertainty;
NRI repatriation timing critical. Consolidate into large-cap quality vehicles to reduce earnings volatility.
→ Consider: ICICI Prudential Bluechip Fund, Mirae Asset Large Cap Fund, Nippon India Large Cap Fund
```

---

## Sample Output — Event 2 (Middle East Tensions / Energy & Currency Volatility)

```
🔔 Market Alert: Escalating Middle East Geopolitical Tensions & US-Iran Friction –
Energy Supply & Currency Volatility

**Priya Mehta (INV001)** | Moderate, age 42, mid-career with school-age children
₹591,240 at risk (49.3% exposure) in HDFC Index Fund and HDFC Flexi Cap Fund.
Geopolitical tensions pressuring industrials and financial services in these broad-market
holdings. Rising oil/USD volatility increases borrowing costs for FMCG suppliers and
infrastructure projects indirectly. Rebalance toward large-cap stability.
→ Consider: ICICI Prudential Bluechip Fund, Mirae Asset Large Cap Fund, Nippon India Large Cap Fund

**Vikram Sharma (INV002)** | Aggressive, age 35, early-career
₹1,774,382 at risk (59.1% exposure) across 6 funds including small/mid-cap heavyweights.
Small-cap illiquidity magnifies sector headwinds (energy, industrials, materials).
Dollar strength erodes export-oriented mid-caps further. Consolidate into resilient large-cap core.
→ Consider: ICICI Prudential Bluechip Fund, Mirae Asset Large Cap Fund, Nippon India Large Cap Fund

**Rajan Kapoor (INV003)** | Conservative, age 61, near retirement
₹440,200 at risk (20.0% exposure) in HDFC Index Fund. Limited exposure cushions this
pre-retirement investor, but broad-market volatility threatens near-term capital preservation.
Shift to income-generating, lower-volatility instruments.
→ Consider: HDFC Corporate Bond Fund, Aditya Birla Sun Life Corporate Bond Fund, Kotak Corporate Bond Fund
```
