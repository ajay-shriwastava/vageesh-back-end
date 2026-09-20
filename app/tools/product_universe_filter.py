"""
product_universe_filter — Pipeline Tool (PIPELINE_TOOLS)

Hard constraint enforcement — deterministic, no LLM.

Reads the portfolio impact report from state, cross-checks every recommended
fund_id against product_catalogue.csv (the ground truth universe), and strips
any fund not found in the universe.

Investors left with zero valid alternatives are NOT silently dropped — they
receive a ⚠️ flag so the RM knows to review them manually.

Reads from state : portfolio_impact  (structured text from portfolio_impact_analyzer)
Writes to state  : validated_impact  (same structure, with non-universe funds removed
                                      and ⚠️ flags added where no alternatives remain)
"""

import csv
import logging
import os
import re

logger = logging.getLogger(__name__)


_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DEFAULT_DATASET_DIR = os.environ.get(
    "DATASET_DIR",
    os.path.normpath(os.path.join(_PROJECT_ROOT, "../../dataset/portfolio")),
)


def _load_valid_fund_ids(catalogue_path: str | None = None) -> set[str]:
    if not catalogue_path:
        catalogue_path = os.path.join(_DEFAULT_DATASET_DIR, "product_catalogue.csv")
    valid: set[str] = set()
    with open(catalogue_path, newline="") as f:
        for row in csv.DictReader(f):
            valid.add(row["fund_id"].strip())
    return valid


def _validate_investor_block(block: str, valid_ids: set[str]) -> tuple[str, int, int]:
    """
    Scan an investor block for recommended fund lines.
    Strip lines referencing fund_ids not in valid_ids.
    Returns (validated_block, kept_count, stripped_count).
    """
    lines = block.splitlines()
    out_lines: list[str] = []
    kept = stripped = 0
    in_alternatives = False

    for line in lines:
        stripped_line = line.strip()

        if "Recommended alternatives" in line:
            in_alternatives = True
            out_lines.append(line)
            continue

        if in_alternatives and stripped_line.startswith("+"):
            # Extract fund_id from pattern: + [MF001] Fund Name ...
            m = re.search(r"\[([A-Z0-9]+)\]", stripped_line)
            if m:
                fid = m.group(1)
                if fid in valid_ids:
                    out_lines.append(line)
                    kept += 1
                else:
                    logger.warning("Stripped non-universe fund: %s", fid)
                    stripped += 1
                continue
            # No fund_id pattern — keep as-is (could be a header line)
            out_lines.append(line)
            continue

        # Check for NONE_IN_UNIVERSE marker
        if "NONE_IN_UNIVERSE" in line:
            in_alternatives = False
            out_lines.append(line)
            continue

        # Any non-alternative line resets the alternatives context
        if in_alternatives and stripped_line and not stripped_line.startswith("+"):
            in_alternatives = False

        out_lines.append(line)

    return "\n".join(out_lines), kept, stripped


async def run(state: dict) -> dict:
    portfolio_impact = state.get("portfolio_impact", "")

    if not portfolio_impact:
        msg = "Product Universe Filter: no portfolio_impact found in state — skipping validation.\n"
        return {
            **state,
            "messages":        list(state.get("messages", [])) + [msg],
            "validated_impact": msg,
            "current_output":  msg,
        }

    catalogue_path = state.get("catalogue_path") or os.path.join(
        state.get("dataset_dir") or _DEFAULT_DATASET_DIR,
        "product_catalogue.csv",
    )
    valid_ids = _load_valid_fund_ids(catalogue_path)
    total_stripped = 0
    filter_log: list[str] = []

    # Split into investor blocks (each starts with "▶")
    # Preserve the header (everything before the first ▶)
    parts = re.split(r"(?=^▶ )", portfolio_impact, flags=re.MULTILINE)
    header = parts[0] if parts else ""
    investor_blocks = parts[1:] if len(parts) > 1 else []

    validated_blocks: list[str] = [header]

    for block in investor_blocks:
        validated_block, kept, stripped = _validate_investor_block(block, valid_ids)
        total_stripped += stripped

        if stripped:
            filter_log.append(
                f"  Stripped {stripped} non-universe fund(s) from: "
                + (block.splitlines()[0] if block.splitlines() else "unknown investor")
            )

        # If all alternatives were stripped (kept=0) and block had alternatives section
        has_alternatives_section = "Recommended alternatives" in block
        no_valid_alternatives     = kept == 0 and has_alternatives_section
        no_universe_marker        = "NONE_IN_UNIVERSE" in block

        if no_valid_alternatives or no_universe_marker:
            # Replace alternatives section with ⚠️ manual review flag
            validated_block = re.sub(
                r"(  Recommended alternatives.*?)(?=\n\n|\Z)",
                "  Recommended alternatives : ⚠️ NO SUITABLE PRODUCT IN UNIVERSE — RM MUST REVIEW MANUALLY",
                validated_block,
                flags=re.DOTALL,
            )

        validated_blocks.append(validated_block)

    result = "".join(validated_blocks)

    # Append filter audit summary
    if total_stripped or filter_log:
        audit = (
            "\n\n--- Product Universe Filter Audit ---\n"
            f"Total non-universe funds stripped: {total_stripped}\n"
        )
        if filter_log:
            audit += "\n".join(filter_log) + "\n"
        result += audit
    else:
        result += "\n\n--- Product Universe Filter: all recommendations validated ✓ ---\n"

    logger.info("Product universe filter: %d funds stripped", total_stripped)
    return {
        **state,
        "messages":        list(state.get("messages", [])) + [result],
        "validated_impact": result,
        "current_output":  result,
    }
