"""
data_quality — Tool node + LangChain @tool

Reads a CSV file and performs quality checks:
  - Detects and removes duplicate rows
  - Rejects completely blank rows
  - Reports per-column partial null counts
"""

import json
import logging
import os
import time

from langchain_core.tools import tool

from app.tools.tool_context import tool_config as _tool_config_var

logger = logging.getLogger(__name__)

DATASET_DIR   = os.environ.get("DATASET_DIR", "")


# ---------------------------------------------------------------------------
# LangChain @tool — used by real agent nodes
# ---------------------------------------------------------------------------

@tool
async def check_data_quality(csv_file_path: str) -> str:
    """Run data quality checks on a CSV file: remove duplicate rows and completely blank rows.
    Writes rejected rows to the error directory and the cleaned data to a staging CSV.
    Returns JSON with: clean_csv_path, raw_rows, kept_rows, duplicates_removed,
    blank_rows_rejected, error_file, warnings.
    Pass the clean_csv_path to ingest_to_db next."""
    import pandas as pd

    cfg = _tool_config_var.get().get("check_data_quality", {})
    dataset_dir = cfg.get("dataset_dir") or DATASET_DIR
    output_dir = os.path.join(dataset_dir, "output")
    error_dir = os.path.join(dataset_dir, "error")

    try:
        df = pd.read_csv(csv_file_path)
    except Exception as exc:
        return json.dumps({"error": f"Could not read CSV: {exc}"})

    raw = len(df)
    dup_mask = df.duplicated(keep="first")
    dups = int(dup_mask.sum())
    df_dedup = df[~dup_mask].reset_index(drop=True)

    null_mask = df_dedup.isnull().all(axis=1)
    blanks = int(null_mask.sum())
    clean_df = df_dedup[~null_mask].reset_index(drop=True)

    stem = os.path.splitext(os.path.basename(csv_file_path))[0]
    ts = time.strftime("%Y%m%d_%H%M%S")

    os.makedirs(output_dir, exist_ok=True)
    clean_path = os.path.join(output_dir, f"{stem}_clean_{ts}.csv")
    clean_df.to_csv(clean_path, index=False)

    error_path = None
    total_rejected = dups + blanks
    if total_rejected > 0:
        orig_df = pd.read_csv(csv_file_path)
        dup_rows = orig_df[orig_df.duplicated(keep="first")].copy()
        blank_rows = df_dedup[null_mask].copy()
        dup_rows["_rejection_reason"] = "duplicate"
        blank_rows["_rejection_reason"] = "blank_row"
        err_df = pd.concat([dup_rows, blank_rows], ignore_index=True)
        os.makedirs(error_dir, exist_ok=True)
        error_path = os.path.join(error_dir, f"{stem}_errors_{ts}.csv")
        err_df.to_csv(error_path, index=False)

    warnings = [
        f"{col}: {int(clean_df[col].isnull().sum())} partial nulls"
        for col in clean_df.columns
        if clean_df[col].isnull().sum() > 0
    ]

    logger.info("Data quality: %d raw, %d kept, %d dups, %d blank", raw, len(clean_df), dups, blanks)
    return json.dumps({
        "clean_csv_path": clean_path,
        "raw_rows": raw,
        "kept_rows": len(clean_df),
        "duplicates_removed": dups,
        "blank_rows_rejected": blanks,
        "error_file": error_path,
        "warnings": warnings,
    })


async def run(state: dict) -> dict:
    import pandas as pd

    file_path = state.get("csv_file_path")
    if not file_path:
        return state

    try:
        df = pd.read_csv(file_path)
    except Exception as exc:
        msg = f"Error reading CSV: {exc}"
        logger.error(msg)
        return {
            **state,
            "messages": list(state.get("messages", [])) + [msg],
        }

    raw_row_count = len(df)

    # --- Duplicate detection ---
    dup_mask = df.duplicated(keep="first")
    duplicates_count = int(dup_mask.sum())
    df = df[~dup_mask].reset_index(drop=True)

    # --- Null detection ---
    # Only reject rows that are completely empty (all columns null).
    # Rows with partial nulls are valid (e.g. optional fields) and pass through.
    null_mask = df.isnull().all(axis=1)
    rows_rejected_count = int(null_mask.sum())
    clean_df = df[~null_mask].reset_index(drop=True)

    # --- Per-column quality issues (informational — partial nulls don't reject rows) ---
    quality_issues = []
    for col in df.columns:
        nc = int(df[col].isnull().sum())
        if nc > 0:
            quality_issues.append({"column": col, "issue": "partial_nulls", "count": nc, "severity": "warning"})
    if duplicates_count > 0:
        quality_issues.append({"column": "ALL", "issue": "duplicate_rows", "count": duplicates_count, "severity": "error"})
    if rows_rejected_count > 0:
        quality_issues.append({"column": "ALL", "issue": "blank_rows", "count": rows_rejected_count, "severity": "error"})

    clean_df_records = clean_df.to_dict(orient="records")
    clean_df_columns = list(clean_df.columns)
    clean_df_dtypes = {col: str(dtype) for col, dtype in clean_df.dtypes.items()}

    # Write rejected rows (nulls + duplicates) to the error directory
    error_file_path = None
    rejected_df = df[null_mask]  # null rows (after dedup)
    total_rejected = duplicates_count + rows_rejected_count

    error_dir = state.get("error_dir")
    csv_filename = state.get("csv_filename", "unknown.csv")
    stem = os.path.splitext(csv_filename)[0]

    if error_dir and total_rejected > 0:
        import time
        ts = time.strftime("%Y%m%d_%H%M%S")
        error_file_path = os.path.join(error_dir, f"{stem}_errors_{ts}.csv")
        try:
            # Re-read original to capture duplicate rows with their reason
            orig_df = pd.read_csv(state["csv_file_path"])
            dup_rows = orig_df[orig_df.duplicated(keep="first")].copy()
            blank_rows = rejected_df.copy()
            dup_rows["_rejection_reason"] = "duplicate"
            blank_rows["_rejection_reason"] = "blank_row"
            error_rows = pd.concat([dup_rows, blank_rows], ignore_index=True)
            error_rows.to_csv(error_file_path, index=False)
            logger.info("Wrote %d rejected rows to %s", len(error_rows), error_file_path)
        except Exception as exc:
            logger.warning("Could not write error file: %s", exc)
            error_file_path = None

    summary = (
        f"Quality check complete: {raw_row_count} raw rows | "
        f"{duplicates_count} duplicates removed | "
        f"{rows_rejected_count} rows rejected (nulls) | "
        f"{len(clean_df)} rows ready to ingest."
    )
    if error_file_path:
        summary += f" | Error file: {os.path.basename(error_file_path)}"
    logger.info(summary)

    return {
        **state,
        "raw_row_count": raw_row_count,
        "duplicates_count": duplicates_count,
        "rows_rejected_count": rows_rejected_count,
        "clean_df_records": clean_df_records,
        "clean_df_columns": clean_df_columns,
        "clean_df_dtypes": clean_df_dtypes,
        "quality_issues": quality_issues,
        "error_file_path": error_file_path,
        "messages": list(state.get("messages", [])) + [summary],
    }
