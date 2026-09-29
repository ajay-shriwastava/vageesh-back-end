"""
Data Ingestion Pipeline — Workflow Template

Triggered by a message (e.g. "process the CSV files" or "run ingestion").
A real agent with tools handles the full pipeline: scan → quality → ingest → profile → report.

Flow: Start → DataIngestionAgent → End
"""

_SYSTEM_PROMPT = (
    "You are a data engineering agent responsible for the full CSV ingestion pipeline. "
    "When asked to run the pipeline, follow these steps in order using your tools:\n"
    "1. Call scan_csv to find the most recently modified CSV in the input directory.\n"
    "2. If no file is found, report that there is nothing to process and stop.\n"
    "3. Call check_data_quality with the csv_file_path from step 1.\n"
    "4. Call ingest_to_db with the clean_csv_path from step 3, the table_name from step 1, "
    "and original_file_path set to the file_path from step 1.\n"
    "5. Call profile_data with the original csv_file_path and table_name.\n"
    "6. Write a concise executive summary covering: which file was processed, ingestion stats "
    "(rows ingested, rejected, duplicates), data quality warnings, and key insights from the profile. "
    "Be specific with numbers. Keep it under 200 words.\n"
    "7. Call publish_report with your summary, the slack_channel from the environment "
    "(use the SLACK_REPORT_CHANNEL env var value or 'data-reports'), csv_filename, "
    "table_name, rows_ingested, rows_rejected, and write_to_file=True.\n"
    "Always complete all steps even if individual steps have minor issues."
)

DATA_INGESTION_TEMPLATE: dict = {
    "id": "data-ingestion-pipeline",
    "name": "Data Ingestion Pipeline",
    "description": (
        "Triggered by a message. A data engineering agent scans for CSV files, "
        "runs quality checks, ingests into PostgreSQL, profiles the data, "
        "and publishes a report to Slack and the output directory."
    ),
    "trigger_type": "message",
    "tool_config_defaults": {
        "csv_scanner":        {"dataset_dir": ""},
        "scan_csv":           {"dataset_dir": ""},
        "data_quality":       {"dataset_dir": ""},
        "check_data_quality": {"dataset_dir": ""},
        "db_ingestor":        {"dataset_dir": ""},
        "ingest_to_db":       {"dataset_dir": ""},
        "data_profiler":      {"dataset_dir": ""},
        "profile_data":       {"dataset_dir": ""},
        "report_publisher":   {"slack_channel": "data-reports", "dataset_dir": ""},
        "publish_report":     {"slack_channel": "data-reports", "dataset_dir": ""},
    },
    # Agent config — used by instantiation to create the agent in the DB
    "agent_config": {
        "name": "Data Ingestion Agent",
        "description": "Orchestrates the full CSV data ingestion pipeline",
        "model": "claude-haiku-4-5-20251001",
        "system_prompt": _SYSTEM_PROMPT,
        "tools": ["scan_csv", "check_data_quality", "ingest_to_db", "profile_data", "publish_report"],
        "channels": [],
        "memory_enabled": False,
    },
    "graph_definition": {
        "nodes": [
            {
                "id": "start",
                "type": "start",
                "label": "Start",
                "x": 40,
                "y": 200,
            },
            {
                "id": "agent",
                "type": "agent",
                "label": "Data Ingestion Agent",
                "agent_id": None,  # filled in by instantiation
                "x": 220,
                "y": 178,
            },
            {
                "id": "end",
                "type": "end",
                "label": "End",
                "x": 420,
                "y": 200,
            },
        ],
        "edges": [
            {"id": "e1", "from": "start", "to": "agent"},
            {"id": "e2", "from": "agent", "to": "end"},
        ],
    },
}
