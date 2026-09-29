"""
Slack bot using Socket Mode.

Listens for:
  - Direct messages (message.im)
  - @mentions (app_mention)

Routes each message to the first agent in the DB that has "slack" in its
channels list. Falls back to a default system prompt if no agent is found.

Persists every inbound and outbound message to the messages table.

MCP enrichment (when MCP_API_KEY is set):
  - Injects agent memory into the system prompt via list_memory MCP tool
  - Injects top-3 relevant knowledge chunks via search_knowledge MCP tool
  - Parses [REMEMBER key: value] in LLM replies and calls set_memory
"""

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from slack_sdk.errors import SlackApiError
from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.agent import Agent
from app.models.message import Message

logger = logging.getLogger(__name__)

_slack_client: SocketModeClient | None = None

# Pattern: [REMEMBER key: value]
_REMEMBER_RE = re.compile(r"\[REMEMBER\s+([^:]+?):\s*(.+?)\]", re.IGNORECASE)

_MCP_URL = "http://localhost:8000/mcp"


# ---------------------------------------------------------------------------
# MCP client helpers (graceful degradation when MCP_API_KEY not set)
# ---------------------------------------------------------------------------

def _mcp_api_key() -> str | None:
    return os.environ.get("MCP_API_KEY", "").strip() or None


async def _mcp_call(tool_name: str, args: dict) -> dict | list | None:
    """
    Call an MCP tool via Streamable HTTP transport.
    Returns parsed JSON result or None on any error.
    Falls back gracefully if MCP_API_KEY is absent.
    """
    api_key = _mcp_api_key()
    if not api_key:
        return None

    try:
        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport

        transport = StreamableHttpTransport(
            url=_MCP_URL,
            headers={"X-MCP-API-Key": api_key},
        )
        async with Client(transport) as client:
            result = await client.call_tool(tool_name, args)
            # FastMCP returns a list of content objects; extract first text item
            if result and hasattr(result[0], "text"):
                return json.loads(result[0].text)
    except Exception as exc:
        logger.warning("MCP call %s failed: %s", tool_name, exc)
    return None


async def _get_memory_context(agent_id: str, caller_id: str) -> str:
    """Return formatted memory block for system prompt injection, or empty string."""
    data = await _mcp_call("list_memory", {"agent_id": agent_id, "caller_id": caller_id})
    if not data or not isinstance(data, list) or len(data) == 0:
        return ""
    lines = "\n".join(f"  {entry['key']}: {entry['value']}" for entry in data)
    return f"Agent Memory:\n{lines}"


async def _get_knowledge_context(user_text: str, caller_id: str) -> str:
    """Return formatted knowledge block for system prompt injection, or empty string."""
    data = await _mcp_call("search_knowledge", {"query": user_text, "caller_id": caller_id, "top_k": 3})
    if not data or not isinstance(data, list) or len(data) == 0:
        return ""
    sections = []
    for chunk in data:
        sections.append(f"[Source: {chunk.get('title', 'unknown')}]\n{chunk.get('content', '')}")
    return "Relevant Knowledge:\n" + "\n---\n".join(sections)


async def _write_remember_markers(agent_id: str, reply: str, caller_id: str) -> str:
    """
    Scan reply for [REMEMBER key: value] markers.
    For each match, call set_memory via MCP.
    Return the reply with all markers stripped.
    """
    if not _mcp_api_key():
        return reply

    matches = _REMEMBER_RE.findall(reply)
    for key, value in matches:
        key = key.strip()
        value = value.strip()
        await _mcp_call("set_memory", {
            "agent_id": agent_id,
            "key": key,
            "value": value,
            "caller_id": caller_id,
        })

    cleaned = _REMEMBER_RE.sub("", reply).strip()
    return cleaned


# ---------------------------------------------------------------------------
# Message persistence
# ---------------------------------------------------------------------------

async def _save_message(role: str, content: str, agent_id: uuid.UUID | None, session_id: uuid.UUID) -> None:
    if agent_id is None:
        return  # agent_id is NOT NULL in schema; skip if no agent assigned
    async with AsyncSessionLocal() as db:
        msg = Message(
            id=uuid.uuid4(),
            session_id=session_id,
            agent_id=agent_id,
            role=role,
            content=content,
        )
        db.add(msg)
        await db.commit()


# ---------------------------------------------------------------------------
# Agent lookup
# ---------------------------------------------------------------------------

async def _get_slack_agent() -> Agent | None:
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(Agent))).scalars().all()
        for agent in rows:
            channels = agent.channels or []
            if any(c.lower() == "slack" for c in channels):
                return agent
    return None


# ---------------------------------------------------------------------------
# Workflow lookup — find a message-triggered workflow for a given agent
# ---------------------------------------------------------------------------

async def _find_message_workflow(agent: Agent):
    """Return the first message-triggered Workflow whose graph contains this agent's id."""
    from app.models.workflow import Workflow
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        workflows = (
            await db.execute(
                select(Workflow).where(Workflow.trigger_type == "message")
            )
        ).scalars().all()

    agent_id_str = str(agent.id)
    for wf in workflows:
        for node in wf.graph_definition.get("nodes", []):
            if node.get("type") == "agent" and str(node.get("agent_id", "")) == agent_id_str:
                return wf
    return None


# ---------------------------------------------------------------------------
# LLM call (fallback — no workflow configured)
# ---------------------------------------------------------------------------

async def _run_agent_direct(agent: Agent | None, user_text: str, slack_user_id: str) -> str:
    model_id = (agent.model if agent else None) or "claude-haiku-4-5-20251001"
    system_prompt = (agent.system_prompt if agent else None) or "You are a helpful assistant."

    caller_id = f"slack:{slack_user_id}"

    # MCP enrichment — fetch memory and relevant knowledge
    if agent:
        agent_id_str = str(agent.id)
        memory_ctx, knowledge_ctx = await asyncio.gather(
            _get_memory_context(agent_id_str, caller_id),
            _get_knowledge_context(user_text, caller_id),
        )
        enrichments = [part for part in [memory_ctx, knowledge_ctx] if part]
        if enrichments:
            system_prompt = "\n\n".join([system_prompt] + enrichments)

    llm = ChatAnthropic(model=model_id)
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_text),
    ]
    result = await llm.ainvoke(messages)
    reply = result.content if hasattr(result, "content") else str(result)

    # Parse and persist [REMEMBER key: value] markers, then strip them
    if agent:
        reply = await _write_remember_markers(str(agent.id), reply, caller_id)

    return reply


# ---------------------------------------------------------------------------
# Workflow execution via message trigger
# ---------------------------------------------------------------------------

async def _run_via_workflow(workflow, agent: Agent, user_text: str) -> str:
    """Execute a message-triggered workflow and return the final text output."""
    import uuid as _uuid
    from app.database import AsyncSessionLocal
    from app.models.workflow_run import WorkflowRun
    from app.workflow_runner import run_workflow

    run_id = str(_uuid.uuid4())
    workflow_id = str(workflow.id)
    agents_map = {str(agent.id): agent}

    async with AsyncSessionLocal() as db:
        run = WorkflowRun(
            id=_uuid.UUID(run_id),
            workflow_id=workflow.id,
            status="pending",
        )
        db.add(run)
        await db.commit()

        await run_workflow(
            run_id=run_id,
            workflow_id=workflow_id,
            graph_definition=workflow.graph_definition,
            agents_map=agents_map,
            input_data={"input": user_text},
            db=db,
        )

        # Re-fetch final state
        from sqlalchemy import select
        run_obj = (
            await db.execute(select(WorkflowRun).where(WorkflowRun.id == _uuid.UUID(run_id)))
        ).scalar_one_or_none()

        if run_obj and run_obj.output:
            msgs = run_obj.output.get("messages", [])
            if msgs:
                return str(msgs[-1])
        return "Workflow completed."


# ---------------------------------------------------------------------------
# Event handler
# ---------------------------------------------------------------------------

async def _handle_event(client: SocketModeClient, req: SocketModeRequest) -> None:
    # Acknowledge immediately — Slack requires ACK within 3 seconds
    await client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))

    payload = req.payload
    event = payload.get("event", {})

    # Ignore bot's own messages
    if event.get("bot_id") or event.get("subtype") == "bot_message":
        return

    user_text = event.get("text", "").strip()
    channel = event.get("channel", "")
    slack_user_id = event.get("user", "unknown")

    if not user_text or not channel:
        return

    # Strip @mention prefix if present (e.g. "<@U12345> hello" → "hello")
    if user_text.startswith("<@"):
        user_text = user_text.split(">", 1)[-1].strip()

    if not user_text:
        return

    session_id = uuid.uuid5(uuid.NAMESPACE_DNS, f"slack:{channel}")

    agent = await _get_slack_agent()
    agent_id = agent.id if agent else None

    # Persist user message
    await _save_message("user", user_text, agent_id, session_id)

    # Route: use a message-triggered workflow if one exists for this agent
    try:
        workflow = await _find_message_workflow(agent) if agent else None
        if workflow:
            logger.info("Routing Slack message to workflow '%s'", workflow.name)
            reply = await _run_via_workflow(workflow, agent, user_text)
        else:
            reply = await _run_agent_direct(agent, user_text, slack_user_id)
    except Exception as exc:
        logger.error("Agent error: %s", exc)
        reply = "Sorry, I encountered an error processing your message."

    # Persist agent reply
    await _save_message("assistant", reply, agent_id, session_id)

    # Send reply to Slack
    web_client: AsyncWebClient = client.web_client
    try:
        await web_client.chat_postMessage(channel=channel, text=reply)
    except SlackApiError as exc:
        logger.error("Slack send error: %s", exc)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def start_slack_bot() -> None:
    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    app_token = os.environ.get("SLACK_APP_TOKEN", "")

    if not bot_token or not app_token:
        logger.warning("SLACK_BOT_TOKEN or SLACK_APP_TOKEN not set — Slack bot disabled.")
        return

    if bot_token.startswith("xoxb-your") or app_token.startswith("xapp-your"):
        logger.warning("Slack tokens are placeholders — Slack bot disabled.")
        return

    global _slack_client

    web_client = AsyncWebClient(token=bot_token)
    _slack_client = SocketModeClient(app_token=app_token, web_client=web_client)
    _slack_client.socket_mode_request_listeners.append(_handle_event)

    try:
        await _slack_client.connect()
        logger.info("Slack bot connected via Socket Mode.")
    except Exception as exc:
        logger.warning("Slack bot failed to connect — bot disabled: %s", exc)
        _slack_client = None


async def ensure_connected() -> None:
    """Attempt to connect the Slack bot if it is not already connected.
    Called before workflow runs that involve a Slack-enabled agent."""
    global _slack_client
    if _slack_client is not None:
        return
    logger.info("Slack bot not connected — attempting reconnect.")
    await start_slack_bot()


async def stop_slack_bot() -> None:
    global _slack_client
    if _slack_client:
        await _slack_client.close()
        _slack_client = None
        logger.info("Slack bot disconnected.")
