"""
portfolio_impact_analyzer — Pipeline Tool (PIPELINE_TOOLS)

Pure deterministic computation — no LLM involved.
Reads the market signal agent's structured output from state, loads the
pre-ingested portfolio and product catalogue CSVs, scores each investor's
weighted sector exposure, and drafts top-3 alternatives from the product universe.

Reads from state : current_output  (market signal agent's structured text)
Writes to state  : portfolio_impact (structured per-investor report)
"""

import csv
import logging
import os
import re

logger = logging.getLogger(__name__)

_MATERIAL_THRESHOLD_PCT = 5.0  # minimum weighted exposure to flag as materially affected


def _dataset_dir() -> str:
    """Read DATASET_DIR at call time so server restarts / .env changes are picked up."""
    _project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    _default = os.path.normpath(os.path.join(_project_root, "../../dataset/portfolio"))
    return os.environ.get("DATASET_DIR", _default)

# Target sub-categories per (risk_profile, sentiment) for alternative recommendations
_RECO_TARGETS: dict[tuple[str, str], set[str]] = {
    ("conservative", "negative"): {"Liquid", "Gilt", "Corporate Bond"},
    ("conservative", "positive"): {"Large Cap", "Index Fund", "Aggressive Hybrid"},
    ("moderate",     "negative"): {"Index Fund", "Large Cap", "Corporate Bond", "Gold FoF"},
    ("moderate",     "positive"): {"Flexi Cap", "Large & Mid Cap", "Mid Cap"},
    ("aggressive",   "negative"): {"Large Cap", "Index Fund", "Flexi Cap"},
    ("aggressive",   "positive"): {"Mid Cap", "Small Cap", "Sectoral - Technology"},
}


def _parse_field(text: str, field: str) -> str:
    """Extract value of a labelled field from the agent's structured output."""
    pattern = rf"^{re.escape(field)}\s*:\s*(.+)$"
    for line in text.splitlines():
        m = re.match(pattern, line.strip(), re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return ""


def _load_catalogue() -> dict[str, dict]:
    path = os.path.join(_dataset_dir(), "product_catalogue.csv")
    catalogue: dict[str, dict] = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            catalogue[row["fund_id"]] = {
                **row,
                "primary_sectors": [s.strip() for s in row["primary_sectors"].split(",")],
            }
    return catalogue


def _load_investors() -> dict[str, dict]:
    path = os.path.join(_dataset_dir(), "sample_portfolios.csv")
    investors: dict[str, dict] = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            inv_id = row["investor_id"]
            if inv_id not in investors:
                investors[inv_id] = {
                    "investor_id":         inv_id,
                    "name":                row["name"],
                    "risk_profile":        row["risk_profile"],
                    "age":                 int(row["age"]),
                    "life_stage":          row["life_stage"],
                    "portfolio_value_inr": float(row["portfolio_value_inr"]),
                    "holdings":            [],
                }
            investors[inv_id]["holdings"].append({
                "fund_id":       row["fund_id"],
                "fund_name":     row["fund_name"],
                "current_value": float(row["current_value_inr"]),
                "weight_pct":    float(row["weight_pct"]),
            })
    return investors


def _sector_overlap(fund_sectors: list[str], affected: set[str]) -> float:
    """Fraction of a fund's primary sectors that overlap with affected sectors (0.0–1.0)."""
    if not fund_sectors:
        return 0.0
    hits = sum(
        1 for fs in fund_sectors
        if any(a.lower() in fs.lower() or fs.lower() in a.lower() for a in affected)
    )
    return hits / len(fund_sectors)


async def run(state: dict) -> dict:
    # --- Parse market signal agent output ---
    agent_output = state.get("current_output") or ""
    affected_sectors_raw = _parse_field(agent_output, "ALL_AFFECTED_SECTORS")
    sentiment_raw        = _parse_field(agent_output, "SENTIMENT")
    event_summary        = _parse_field(agent_output, "EVENT_SUMMARY")

    affected_set: set[str] = {s.strip() for s in affected_sectors_raw.split(",") if s.strip()}
    sentiment_key = "negative" if "negative" in sentiment_raw.lower() else "positive"

    if not affected_set:
        msg = (
            "Portfolio Impact Analysis\n"
            "=========================\n"
            "Could not parse affected sectors from market signal output.\n"
            "No impact analysis performed.\n"
        )
        return {
            **state,
            "messages":         list(state.get("messages", [])) + [msg],
            "portfolio_impact": msg,
        }

    # --- Load pre-ingested data ---
    catalogue = _load_catalogue()
    investors  = _load_investors()

    lines = [
        "Portfolio Impact Analysis",
        f"Event    : {event_summary}",
        f"Sectors  : {affected_sectors_raw}",
        f"Sentiment: {'NEGATIVE (risk-off)' if sentiment_key == 'negative' else 'POSITIVE (risk-on)'}",
        "=" * 60,
        "",
    ]

    impacted_ids: list[str] = []

    for inv_id, inv in sorted(investors.items()):
        holding_impacts: list[dict] = []
        total_weighted_exposure = 0.0

        for h in inv["holdings"]:
            fund = catalogue.get(h["fund_id"])
            if not fund:
                continue
            overlap = _sector_overlap(fund["primary_sectors"], affected_set)
            if overlap > 0:
                net_exposure = round(h["weight_pct"] * overlap, 2)
                total_weighted_exposure += net_exposure
                holding_impacts.append({
                    "fund_id":      h["fund_id"],
                    "fund_name":    h["fund_name"],
                    "weight_pct":   h["weight_pct"],
                    "overlap_pct":  round(overlap * 100),
                    "net_exposure": net_exposure,
                    "sub_category": fund.get("sub_category", ""),
                })

        if total_weighted_exposure < _MATERIAL_THRESHOLD_PCT:
            continue

        impacted_ids.append(inv_id)
        affected_value = inv["portfolio_value_inr"] * total_weighted_exposure / 100

        # Pick top-3 alternatives: not already held, matching (risk_profile, sentiment)
        held_ids      = {h["fund_id"] for h in inv["holdings"]}
        risk_profile  = inv["risk_profile"].lower()
        target_cats   = _RECO_TARGETS.get((risk_profile, sentiment_key), set())
        alternatives  = [
            f for fid, f in catalogue.items()
            if fid not in held_ids and f.get("sub_category", "") in target_cats
        ][:3]

        lines += [
            f"▶ {inv['name']} ({inv_id})  |  {inv['risk_profile']}  |  Age {inv['age']}",
            f"  Life stage        : {inv['life_stage']}",
            f"  Portfolio value   : ₹{inv['portfolio_value_inr']:>12,.0f}",
            f"  Affected exposure : {total_weighted_exposure:.1f}%  ≈  ₹{affected_value:,.0f}",
            f"  Impacted holdings :",
        ]
        for h in sorted(holding_impacts, key=lambda x: -x["net_exposure"]):
            lines.append(
                f"    - [{h['fund_id']}] {h['fund_name']}"
                f"  ({h['weight_pct']:.1f}% weight, {h['overlap_pct']}% sector overlap"
                f" → {h['net_exposure']:.1f}% net exposure)"
            )

        if alternatives:
            lines.append("  Recommended alternatives (product universe only) :")
            for a in alternatives:
                lines.append(
                    f"    + [{a['fund_id']}] {a['fund_name']}"
                    f"  [{a['sub_category']}]"
                    f"  |  Expense: {a['expense_ratio_pct']}%"
                    f"  |  Risk: {a['risk_grade']}"
                    f"  |  AUM: ₹{float(a['aum_cr']):,.0f} Cr"
                )
        else:
            lines.append("  Recommended alternatives : NONE_IN_UNIVERSE")

        lines.append("")

    summary = (
        f"Materially impacted: {len(impacted_ids)} of {len(investors)} investors"
        f"  (threshold >{_MATERIAL_THRESHOLD_PCT}% weighted exposure)"
    )
    lines.insert(5, summary)
    lines.insert(6, "")

    if not impacted_ids:
        lines.append("No investors are materially affected by this market event.")

    result = "\n".join(lines)
    logger.info(
        "Portfolio impact: %d/%d investors affected | sectors=%s",
        len(impacted_ids), len(investors), affected_sectors_raw,
    )
    return {
        **state,
        "messages":         list(state.get("messages", [])) + [result],
        "portfolio_impact": result,
        "current_output":   result,
    }
