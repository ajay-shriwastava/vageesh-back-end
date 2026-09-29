"""
SRE Job Summary — Workflow Template

Two-agent handoff pipeline:
  1. Stats Collector  — calls collect_job_stats and returns raw statistics
  2. SRE Report Writer — receives the stats, formats a Slack summary, publishes it

Flow: Start → stats-agent → report-agent → End

The handoff between the two agents is persisted to the messages table (role="agent")
and visible in the Messages UI under the Agent Handoffs tab.
"""

_STATS_PROMPT = (
    "You are a job-stats collector agent. "
    "When invoked, call collect_job_stats with lookback_hours=24 to retrieve workflow run statistics. "
    "Return the raw statistics as a structured plain-text summary with these sections:\n"
    "- Total runs and overall success rate\n"
    "- Counts: completed / failed / running / pending\n"
    "- Per-workflow breakdown (name, total runs, completed, failed)\n"
    "Do NOT format for Slack. Do NOT publish anything. Just return the data clearly."
)

_REPORT_PROMPT = (
    "You are an SRE report-writer agent. "
    "You will receive raw job statistics collected by the Stats Collector agent. "
    "Your job is to:\n"
    "1. Format the stats into a concise Slack-friendly bullet-point health summary "
    "(under 300 words, use ✅ healthy, ❌ failures, ⚠️ attention needed).\n"
    "2. Call publish_report with your formatted summary, slack_channel='job-summary', "
    "and write_to_file=False.\n"
    "Always publish the report — never skip step 2."
)

SRE_REPORT_TEMPLATE: dict = {
    "id": "sre-job-summary",
    "name": "SRE Job Summary",
    "description": (
        "Two-agent handoff: Stats Collector gathers raw workflow run data, "
        "then hands off to SRE Report Writer which formats and publishes to #job-summary."
    ),
    "trigger_type": "message",
    "tool_config_defaults": {
        "publish_report": {"slack_channel": "job-summary", "dataset_dir": ""},
    },
    # agent_configs: list — each entry is mapped to a specific graph node by node_id
    "agent_configs": [
        {
            "node_id": "stats-agent",
            "name": "Stats Collector",
            "description": "Collects raw workflow run statistics for the last 24 hours",
            "model": "claude-haiku-4-5-20251001",
            "system_prompt": _STATS_PROMPT,
            "tools": ["collect_job_stats"],
            "channels": [],
            "memory_enabled": False,
        },
        {
            "node_id": "report-agent",
            "name": "SRE Report Writer",
            "description": "Formats stats from Stats Collector and publishes to Slack",
            "model": "claude-haiku-4-5-20251001",
            "system_prompt": _REPORT_PROMPT,
            "tools": ["publish_report"],
            "channels": [],
            "memory_enabled": False,
        },
    ],
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
                "id": "stats-agent",
                "type": "agent",
                "label": "Stats Collector",
                "agent_id": None,  # filled in by instantiation
                "x": 220,
                "y": 178,
            },
            {
                "id": "report-agent",
                "type": "agent",
                "label": "SRE Report Writer",
                "agent_id": None,  # filled in by instantiation
                "x": 440,
                "y": 178,
            },
            {
                "id": "end",
                "type": "end",
                "label": "End",
                "x": 640,
                "y": 200,
            },
        ],
        "edges": [
            {"id": "e1", "from": "start",        "to": "stats-agent"},
            {"id": "e2", "from": "stats-agent",  "to": "report-agent"},
            {"id": "e3", "from": "report-agent", "to": "end"},
        ],
    },
}
