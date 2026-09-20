"""
report_publisher — Tool node + LangChain @tool

Publishes a report by:
  1. Writing it to a .txt file in the output/ directory (optional)
  2. Posting it to a Slack channel
"""

import logging
import os
import time

from langchain_core.tools import tool

from app.tools.tool_context import tool_config as _tool_config_var

logger = logging.getLogger(__name__)

SLACK_REPORT_CHANNEL = os.environ.get("SLACK_REPORT_CHANNEL", "")
DATASET_DIR = os.environ.get("DATASET_DIR", "")


# ---------------------------------------------------------------------------
# LangChain @tool — used by real agent nodes
# ---------------------------------------------------------------------------

@tool
async def publish_report(
    report_text: str,
    slack_channel: str = "",
    csv_filename: str = "report",
    table_name: str = "",
    rows_ingested: int = 0,
    rows_rejected: int = 0,
    write_to_file: bool = True,
) -> str:
    """Publish a report: write it to a .txt file in the output directory and/or post to Slack.
    slack_channel: Slack channel name (without #). Falls back to SLACK_REPORT_CHANNEL env var.
    write_to_file: set to False for reports that don't need a file (e.g. SRE summaries).
    Returns confirmation of what was published."""
    cfg = _tool_config_var.get().get("publish_report", {})
    dataset_dir = cfg.get("dataset_dir") or DATASET_DIR
    output_dir = os.path.join(dataset_dir, "output")
    channel = slack_channel or cfg.get("slack_channel") or SLACK_REPORT_CHANNEL
    ts = time.strftime("%Y%m%d_%H%M%S")
    stem = os.path.splitext(csv_filename)[0] if csv_filename else "report"

    report_file_path = None
    if write_to_file:
        os.makedirs(output_dir, exist_ok=True)
        report_file_path = os.path.join(output_dir, f"{stem}_report_{ts}.txt")
        header = (
            f"Data Ingestion Report\n"
            f"{'=' * 60}\n"
            f"File       : {csv_filename}\n"
            f"Table      : {table_name}\n"
            f"Timestamp  : {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Ingested   : {rows_ingested} rows\n"
            f"Rejected   : {rows_rejected} rows\n"
            f"{'=' * 60}\n\n"
        )
        try:
            with open(report_file_path, "w") as f:
                f.write(header + report_text)
            logger.info("Report written to %s", report_file_path)
        except Exception as exc:
            logger.warning("Could not write report file: %s", exc)
            report_file_path = None

    slack_sent = False
    if channel:
        try:
            from slack_sdk.web.async_client import AsyncWebClient
            bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
            if bot_token:
                slack_msg = report_text[:2800]
                if table_name:
                    slack_msg = (
                        f"*Report — {csv_filename or table_name}*\n"
                        f"> Table: `{table_name}` | Ingested: *{rows_ingested}* | Rejected: *{rows_rejected}*\n\n"
                        + slack_msg
                    )
                client = AsyncWebClient(token=bot_token)
                await client.chat_postMessage(channel=channel, text=slack_msg)
                logger.info("Report posted to Slack #%s", channel)
                slack_sent = True
        except Exception as exc:
            logger.warning("Could not post to Slack: %s", exc)

    parts = []
    if report_file_path:
        parts.append(f"file written: {os.path.basename(report_file_path)}")
    if slack_sent:
        parts.append(f"posted to Slack #{channel}")
    if not parts:
        parts.append("no output destination configured (set SLACK_BOT_TOKEN / SLACK_REPORT_CHANNEL)")

    return "Report published — " + ", ".join(parts)


async def run(state: dict) -> dict:
    messages = state.get("messages", [])
    report_text = messages[-1] if messages else "No report generated."

    csv_filename  = state.get("csv_filename", "unknown.csv")
    rows_ingested = state.get("rows_ingested", 0)
    rows_rejected = state.get("rows_rejected_count", 0)
    duplicates    = state.get("duplicates_count", 0)
    table_name    = state.get("table_name", "")
    output_dir    = state.get("output_dir") or os.path.join(
        state.get("dataset_dir") or DATASET_DIR, "output"
    )

    ts   = time.strftime("%Y%m%d_%H%M%S")
    stem = os.path.splitext(csv_filename)[0]

    # Channel: state wins over env var
    slack_channel = state.get("slack_channel") or SLACK_REPORT_CHANNEL
    # Allow templates to skip file writing (e.g. SRE report has no output_dir)
    write_report_file = state.get("write_report_file", True)

    # ------------------------------------------------------------------
    # 1. Write report to output/
    # ------------------------------------------------------------------
    report_file_path = None
    if output_dir and write_report_file:
        os.makedirs(output_dir, exist_ok=True)
        report_file_path = os.path.join(output_dir, f"{stem}_report_{ts}.txt")
        header = (
            f"Data Ingestion Report\n"
            f"{'=' * 60}\n"
            f"File       : {csv_filename}\n"
            f"Table      : {table_name}\n"
            f"Timestamp  : {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Ingested   : {rows_ingested} rows\n"
            f"Rejected   : {rows_rejected} rows\n"
            f"Duplicates : {duplicates} rows\n"
            f"{'=' * 60}\n\n"
        )
        try:
            with open(report_file_path, "w") as f:
                f.write(header + report_text)
            logger.info("Report written to %s", report_file_path)
        except Exception as exc:
            logger.warning("Could not write report file: %s", exc)
            report_file_path = None

    # ------------------------------------------------------------------
    # 2. Post to Slack
    # ------------------------------------------------------------------
    slack_sent = False
    if slack_channel:
        try:
            from slack_sdk.web.async_client import AsyncWebClient
            bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
            if bot_token:
                slack_msg = (
                    f"*Data Ingestion Report — {csv_filename}*\n"
                    f"> Table: `{table_name}` | "
                    f"Ingested: *{rows_ingested}* | "
                    f"Rejected: *{rows_rejected}* | "
                    f"Duplicates: *{duplicates}*\n\n"
                    + report_text[:2800]  # Slack message limit safety
                )
                client = AsyncWebClient(token=bot_token)
                await client.chat_postMessage(
                    channel=slack_channel,
                    text=slack_msg,
                )
                logger.info("Report posted to Slack channel: %s", slack_channel)
                slack_sent = True
            else:
                logger.warning("SLACK_BOT_TOKEN not set — skipping Slack report.")
        except Exception as exc:
            logger.warning("Could not post report to Slack: %s", exc)
    else:
        logger.info("SLACK_REPORT_CHANNEL not set — skipping Slack report.")

    summary = f"Report published."
    if report_file_path:
        summary += f" File: {os.path.basename(report_file_path)}."
    if slack_sent:
        summary += f" Slack: {SLACK_REPORT_CHANNEL}."

    return {
        **state,
        "report_file_path": report_file_path,
        "slack_sent": slack_sent,
        "messages": list(state.get("messages", [])) + [summary],
    }
