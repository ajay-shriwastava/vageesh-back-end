"""
db_ingestor — Tool node + LangChain @tool

Creates a PostgreSQL table matching the CSV schema (if it doesn't exist)
and inserts clean rows. Moves the original file to processed/.
"""

import json
import logging
import math
import os
import time

from langchain_core.tools import tool

from app.tools.tool_context import tool_config as _tool_config_var

logger = logging.getLogger(__name__)

DATASET_DIR   = os.environ.get("DATASET_DIR", "")

# pandas dtype string → PostgreSQL type
_DTYPE_MAP: dict[str, str] = {
    "int64":            "BIGINT",
    "int32":            "INTEGER",
    "int16":            "SMALLINT",
    "float64":          "DOUBLE PRECISION",
    "float32":          "REAL",
    "bool":             "BOOLEAN",
    "object":           "TEXT",
    "datetime64[ns]":   "TIMESTAMP",
    "datetime64[ns, UTC]": "TIMESTAMPTZ",
}


def _pg_type(dtype_str: str) -> str:
    return _DTYPE_MAP.get(dtype_str, "TEXT")


def _quoted(name: str) -> str:
    return '"' + name.replace('"', "") + '"'


# ---------------------------------------------------------------------------
# LangChain @tool — used by real agent nodes
# ---------------------------------------------------------------------------

@tool
async def ingest_to_db(clean_csv_path: str, table_name: str, original_file_path: str = "") -> str:
    """Ingest a cleaned CSV file into a PostgreSQL table. Creates the table if it doesn't exist.
    Moves original_file_path to the processed directory when provided.
    Deletes the clean staging CSV after ingestion.
    Returns JSON with: rows_ingested, output_csv_path, table_name, error (if any)."""
    import pandas as pd
    from app.database import AsyncSessionLocal
    from sqlalchemy import text

    cfg = _tool_config_var.get().get("ingest_to_db", {})
    dataset_dir = cfg.get("dataset_dir") or DATASET_DIR
    output_dir = os.path.join(dataset_dir, "output")
    processed_dir = os.path.join(dataset_dir, "processed")

    try:
        df = pd.read_csv(clean_csv_path)
    except Exception as exc:
        return json.dumps({"error": f"Could not read clean CSV: {exc}"})

    columns = list(df.columns)
    dtypes = {col: str(df[col].dtype) for col in columns}
    records = df.to_dict(orient="records")

    col_defs = ", ".join(
        f"{_quoted(col)} {_pg_type(dtypes.get(col, 'object'))}" for col in columns
    )
    create_sql = f'CREATE TABLE IF NOT EXISTS {_quoted(table_name)} ({col_defs});'
    rows_ingested = 0

    async with AsyncSessionLocal() as db:
        try:
            await db.execute(text(create_sql))
            await db.commit()
            cols_str = ", ".join(_quoted(c) for c in columns)
            placeholders = ", ".join(f":p{i}" for i in range(len(columns)))
            insert_sql = f"INSERT INTO {_quoted(table_name)} ({cols_str}) VALUES ({placeholders})"
            for record in records:
                params = {
                    f"p{i}": (
                        None
                        if (v := record.get(col)) is not None and isinstance(v, float) and math.isnan(v)
                        else v
                    )
                    for i, col in enumerate(columns)
                }
                await db.execute(text(insert_sql), params)
                rows_ingested += 1
            await db.commit()
        except Exception as exc:
            logger.error("DB ingest error for table '%s': %s", table_name, exc)
            await db.rollback()
            # Still move original to processed so it isn't retried
            if original_file_path and os.path.isfile(original_file_path):
                _move_file_to_processed(original_file_path, processed_dir)
            try:
                os.remove(clean_csv_path)
            except Exception:
                pass
            return json.dumps({"error": str(exc), "rows_ingested": 0})

    # Write ingested rows to a permanent output CSV
    stem = os.path.splitext(os.path.basename(clean_csv_path))[0].replace("_clean_", "_")
    ts = time.strftime("%Y%m%d_%H%M%S")
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"{stem}_ingested_{ts}.csv")
    try:
        import csv as csv_mod
        with open(output_path, "w", newline="") as f:
            writer = csv_mod.DictWriter(f, fieldnames=columns)
            writer.writeheader()
            writer.writerows(records)
    except Exception as exc:
        logger.warning("Could not write output CSV: %s", exc)
        output_path = None

    # Move original to processed
    if original_file_path and os.path.isfile(original_file_path):
        _move_file_to_processed(original_file_path, processed_dir)

    # Delete the clean staging CSV
    try:
        os.remove(clean_csv_path)
    except Exception:
        pass

    logger.info("Ingested %d rows into '%s'", rows_ingested, table_name)
    return json.dumps({
        "rows_ingested": rows_ingested,
        "table_name": table_name,
        "output_csv_path": output_path,
    })


def _move_file_to_processed(file_path: str, processed_dir: str | None = None) -> None:
    """Move a file to processed_dir (or PROCESSED_DIR fallback) with a timestamp suffix."""
    stem = os.path.splitext(os.path.basename(file_path))[0]
    dest_dir = processed_dir or os.path.join(DATASET_DIR, "processed")
    os.makedirs(dest_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(dest_dir, f"{stem}_processed_{ts}.csv")
    try:
        os.rename(file_path, dest)
        logger.info("Moved to processed: %s", dest)
    except Exception as exc:
        logger.warning("Could not move to processed: %s", exc)


def _move_to_processed(state: dict) -> None:
    """Move the input file to processed/ regardless of ingest outcome."""
    input_path = state.get("csv_file_path")
    processed_dir = state.get("processed_dir")
    csv_filename = state.get("csv_filename", "unknown.csv")
    stem = os.path.splitext(csv_filename)[0]

    if not input_path or not processed_dir:
        logger.warning("Cannot move to processed — input_path=%r processed_dir=%r", input_path, processed_dir)
        return
    if not os.path.isfile(input_path):
        logger.warning("Input file no longer exists, skipping move: %s", input_path)
        return

    os.makedirs(processed_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(processed_dir, f"{stem}_processed_{ts}.csv")
    try:
        os.rename(input_path, dest)
        logger.info("Moved input file to processed: %s", dest)
    except Exception as exc:
        logger.warning("Could not move input file to processed: %s", exc)


async def run(state: dict) -> dict:
    from app.database import AsyncSessionLocal
    from sqlalchemy import text

    table_name = state.get("table_name")
    records: list[dict] = state.get("clean_df_records", [])
    columns: list[str] = state.get("clean_df_columns", [])
    dtypes: dict[str, str] = state.get("clean_df_dtypes", {})

    if not table_name or not records:
        return {**state, "rows_ingested": 0}

    col_defs = ", ".join(
        f"{_quoted(col)} {_pg_type(dtypes.get(col, 'object'))}"
        for col in columns
    )
    create_sql = f'CREATE TABLE IF NOT EXISTS {_quoted(table_name)} ({col_defs});'

    rows_ingested = 0

    async with AsyncSessionLocal() as db:
        try:
            await db.execute(text(create_sql))
            await db.commit()

            cols_str = ", ".join(_quoted(c) for c in columns)
            placeholders = ", ".join(f":p{i}" for i in range(len(columns)))
            insert_sql = (
                f"INSERT INTO {_quoted(table_name)} ({cols_str}) VALUES ({placeholders})"
            )
            for record in records:
                params = {
                    f"p{i}": (
                        None
                        if (v := record.get(col)) is not None and isinstance(v, float) and math.isnan(v)
                        else v
                    )
                    for i, col in enumerate(columns)
                }
                await db.execute(text(insert_sql), params)
                rows_ingested += 1

            await db.commit()

        except Exception as exc:
            logger.error("DB ingest error for table '%s': %s", table_name, exc)
            await db.rollback()
            _move_to_processed(state)
            msg = f"Ingest error: {exc}"
            return {
                **state,
                "rows_ingested": 0,
                "ingest_error": str(exc),
                "messages": list(state.get("messages", [])) + [msg],
            }

    # Write ingested rows to output directory as CSV
    output_file_path = None
    output_dir = state.get("output_dir")
    csv_filename = state.get("csv_filename", "unknown.csv")
    stem = os.path.splitext(csv_filename)[0]

    if output_dir and records:
        ts = time.strftime("%Y%m%d_%H%M%S")
        output_file_path = os.path.join(output_dir, f"{stem}_ingested_{ts}.csv")
        try:
            import csv as csv_mod
            with open(output_file_path, "w", newline="") as f:
                writer = csv_mod.DictWriter(f, fieldnames=columns)
                writer.writeheader()
                writer.writerows(records)
            logger.info("Wrote %d ingested rows to %s", rows_ingested, output_file_path)
        except Exception as exc:
            logger.warning("Could not write output file: %s", exc)
            output_file_path = None

    # Move the input file to processed/ so it isn't re-processed on the next run
    _move_to_processed(state)

    summary = (
        f"Ingested {rows_ingested} rows into table '{table_name}'. "
        f"Rejected: {state.get('rows_rejected_count', 0)} rows. "
        f"Duplicates removed: {state.get('duplicates_count', 0)}."
    )
    if output_file_path:
        summary += f" | Output: {os.path.basename(output_file_path)}"
    logger.info(summary)

    return {
        **state,
        "rows_ingested": rows_ingested,
        "output_file_path": output_file_path,
        "messages": list(state.get("messages", [])) + [summary],
    }
