"""
WorkflowRunner: compiles a graph_definition dict into a LangGraph StateGraph
and executes it, broadcasting WebSocket events at each step.

State keys:
    messages        List[str]   — accumulated text outputs
    current_output  Any         — output of the last node
    condition_result bool       — used by condition nodes for routing
    run_id          str         — UUID string of the current WorkflowRun
"""

import asyncio
import logging
import os
import re
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph
from sqlalchemy.ext.asyncio import AsyncSession

from app.ws_manager import manager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Message log level
# ---------------------------------------------------------------------------

_LEVEL_RANK: dict[str, int] = {"MINIMAL": 0, "STANDARD": 1, "VERBOSE": 2}


def _effective_log_level(agent_obj) -> str:
    """Return effective log level: max(env floor, agent-level setting).

    Never raises. Unknown values fall back to MINIMAL.
    """
    floor = os.environ.get("MESSAGE_LOG_LEVEL", "MINIMAL").upper()
    if floor not in _LEVEL_RANK:
        floor = "MINIMAL"
    agent_level: str | None = None
    if agent_obj is not None:
        agent_level = (
            agent_obj.message_log_level
            if hasattr(agent_obj, "message_log_level")
            else (agent_obj.get("message_log_level") if isinstance(agent_obj, dict) else None)
        )
    if agent_level and agent_level.upper() in _LEVEL_RANK:
        agent_level = agent_level.upper()
        return agent_level if _LEVEL_RANK[agent_level] > _LEVEL_RANK[floor] else floor
    return floor


# ---------------------------------------------------------------------------
# Pricing (per million tokens: input_rate, output_rate in USD)
# ---------------------------------------------------------------------------

_PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5":  (0.80,  4.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4-6":  (15.00, 75.00),
}

def _estimate_cost(model_id: str, input_tokens: int, output_tokens: int) -> float:
    for prefix, (in_rate, out_rate) in _PRICING.items():
        if prefix in model_id:
            return round((input_tokens * in_rate + output_tokens * out_rate) / 1_000_000, 6)
    return round((input_tokens * 3.00 + output_tokens * 15.00) / 1_000_000, 6)


def _zero_usage() -> dict:
    return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "estimated_cost_usd": 0.0}


# ---------------------------------------------------------------------------
# Rate limiting — per-agent sliding window (in-memory, single-server)
# ---------------------------------------------------------------------------

_rate_limit_windows: dict[str, deque] = {}


def _check_rate_limit(agent_id: str, limit: int) -> bool:
    """Return True if the call is within the rate limit, False if exceeded."""
    now = time.monotonic()
    window = _rate_limit_windows.setdefault(agent_id, deque())
    while window and now - window[0] > 60.0:
        window.popleft()
    if len(window) >= limit:
        return False
    window.append(now)
    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _broadcast(run_id: str, event: dict) -> None:
    await manager.broadcast(run_id, event)


async def _write_log(
    level: str,
    message: str,
    workflow_id: str | None = None,
    agent_id: str | None = None,
) -> None:
    from app.database import AsyncSessionLocal
    from app.models.log import Log
    try:
        async with AsyncSessionLocal() as db:
            log = Log(
                id=uuid.uuid4(),
                level=level,
                message=message,
                workflow_id=uuid.UUID(workflow_id) if workflow_id else None,
                agent_id=uuid.UUID(agent_id) if agent_id else None,
            )
            db.add(log)
            await db.commit()
    except Exception as exc:
        logger.warning("Failed to persist log: %s", exc)


# ---------------------------------------------------------------------------
# System prompt template resolver
# ---------------------------------------------------------------------------

def _resolve_system_prompt(template: str, **variables: str) -> str:
    """Replace {variable} placeholders in a system prompt with resolved values.

    Only known variables are substituted — unknown placeholders (e.g. JSON
    examples like {field: value}) are left unchanged so they don't cause errors.
    """
    def _replacer(match: re.Match) -> str:
        key = match.group(1)
        return variables.get(key, match.group(0))

    return re.sub(r"\{(\w+)\}", _replacer, template)


# ---------------------------------------------------------------------------
# Node factories
# ---------------------------------------------------------------------------

def _make_start_node(run_id: str):
    async def start_node(state: dict) -> dict:
        await _broadcast(run_id, {"event": "node_enter", "node_id": "start", "ts": _now_iso()})
        await _broadcast(run_id, {"event": "node_complete", "node_id": "start", "output": {}, "ts": _now_iso()})
        return state

    return start_node


def _make_end_node(run_id: str):
    async def end_node(state: dict) -> dict:
        await _broadcast(run_id, {"event": "node_enter", "node_id": "end", "ts": _now_iso()})
        await _broadcast(run_id, {"event": "node_complete", "node_id": "end", "output": {}, "ts": _now_iso()})
        return state

    return end_node


MAX_LOOPS = 20  # max times a feedback loop may repeat before forcing exit


def _make_agent_node(
    node_id: str,
    system_prompt: str,
    model_id: str,
    run_id: str,
    workflow_id: str | None = None,
    agent_id_str: str | None = None,
    agent_obj=None,
    max_loops: int = MAX_LOOPS,
    next_node_type: str | None = None,
    next_agent_id: str | None = None,
    workflow_tool_config: dict | None = None,
):
    """
    Build an agent node. When agent_obj is provided and has tools configured,
    uses create_react_agent for a real ReAct loop with tool calling.
    Falls back to a simple single-turn LLM call otherwise.
    """
    async def agent_node(state: dict) -> dict:
        await _broadcast(run_id, {"event": "node_enter", "node_id": node_id, "ts": _now_iso()})

        resolved_system = system_prompt or ""
        resolved_model = model_id or "claude-haiku-4-5-20251001"
        bound_tools: list = []
        max_tokens_param: int | None = None
        temperature_param: float | None = None
        max_turns_param: int = 10

        # Compute user_input early so the input guard can inspect it
        history = state.get("messages", [])
        user_input = history[-1] if history else state.get("input", "Begin.")
        _input_rejected: str | None = None

        if agent_obj is not None:
            from app.tools import TOOL_REGISTRY
            from app.models.memory import AgentMemory
            from sqlalchemy import select as sa_select
            from app.database import AsyncSessionLocal

            agent_id = getattr(agent_obj, "id", None)

            # 1. Load memory entries
            memory_enabled = (
                agent_obj.memory_enabled
                if hasattr(agent_obj, "memory_enabled")
                else agent_obj.get("memory_enabled", False)
            )
            mem_text = ""
            if memory_enabled and agent_id:
                try:
                    async with AsyncSessionLocal() as db:
                        rows = (await db.execute(
                            sa_select(AgentMemory).where(AgentMemory.agent_id == agent_id)
                        )).scalars().all()
                        if rows:
                            mem_text = "\n".join(f"{r.key}: {r.value}" for r in rows)
                except Exception as exc:
                    logger.warning("Could not load agent memory: %s", exc)

            # 2. Resolve template variables in system prompt
            agent_name = (
                agent_obj.name if hasattr(agent_obj, "name") else agent_obj.get("name", "")
            ) or ""
            resolved_system = _resolve_system_prompt(
                resolved_system,
                agent_name=agent_name,
                current_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                memory=mem_text,
            )

            # 3. Backward compat: append memory if {memory} not used in template
            if memory_enabled and mem_text and "{memory}" not in (system_prompt or ""):
                resolved_system += f"\n\nYour current memory:\n{mem_text}"

            # 3b. Instruct the agent to use memory tools when memory is enabled
            if memory_enabled and agent_id:
                resolved_system += (
                    "\n\nYou have persistent memory across runs. "
                    "Use the remember(key, value) tool to store any fact worth retaining for future runs — "
                    "such as user preferences, decisions made, or important context. "
                    "Use forget(key) to remove a fact that is no longer accurate."
                )

            # 4. Guardrails — appended after template resolution (safety boundary)
            guardrails = (
                agent_obj.guardrails
                if hasattr(agent_obj, "guardrails")
                else agent_obj.get("guardrails", {})
            ) or {}

            # 4a. restricted_topics → soft system-prompt guard
            restricted = guardrails.get("restricted_topics", [])
            if restricted:
                resolved_system += f"\n\nDo NOT discuss: {', '.join(restricted)}."

            # 4b. content_filter_level → system-prompt hardening
            filter_level = guardrails.get("content_filter_level", "off")
            if filter_level == "low":
                resolved_system += "\n\nAvoid generating harmful, explicit, or offensive content."
            elif filter_level == "medium":
                resolved_system += (
                    "\n\nDo not generate harmful, explicit, offensive, or inappropriate content. "
                    "If asked to do so, politely decline."
                )
            elif filter_level == "high":
                resolved_system += (
                    "\n\nStrict content policy: Refuse any request that could be harmful, "
                    "explicit, offensive, illegal, or inappropriate. Err on the side of caution."
                )

            # 4c. max_tokens_per_response → passed to the LLM constructor
            max_tokens_val = guardrails.get("max_tokens_per_response")
            if max_tokens_val and isinstance(max_tokens_val, int) and max_tokens_val > 0:
                max_tokens_param = max_tokens_val

            # 4d. rate_limit_per_minute → sliding-window check before LLM call
            rate_limit = guardrails.get("rate_limit_per_minute", 0)
            if rate_limit and agent_id_str:
                if not _check_rate_limit(agent_id_str, rate_limit):
                    raise RuntimeError(
                        f"Rate limit exceeded: agent is allowed {rate_limit} calls/min."
                    )

            # 4e. restricted_topics input guard — hard block before LLM call
            if restricted:
                user_input_lower = str(user_input).lower()
                for topic in restricted:
                    if topic.lower() in user_input_lower:
                        _input_rejected = "I'm sorry, I'm not able to discuss that topic."
                        logger.info(
                            "Node '%s' input rejected — restricted topic detected.", node_id
                        )
                        break

            # 5. Bind tools + auto-add remember/forget when memory is enabled
            tool_names: list = (
                agent_obj.tools
                if hasattr(agent_obj, "tools")
                else agent_obj.get("tools", [])
            ) or []
            bound_tools = [TOOL_REGISTRY[t] for t in tool_names if t in TOOL_REGISTRY]
            if memory_enabled and agent_id:
                from app.tools.memory_tools import make_memory_tools
                bound_tools = bound_tools + make_memory_tools(str(agent_id))

            # 6. Auto-inject channel tools for each active channel
            from app.tools import CHANNEL_TOOLS
            active_channels: list = (
                agent_obj.channels
                if hasattr(agent_obj, "channels")
                else agent_obj.get("channels", [])
            ) or []
            already_bound = {t.name for t in bound_tools}
            for channel in active_channels:
                for tool_name in CHANNEL_TOOLS.get(channel, []):
                    if tool_name in TOOL_REGISTRY and tool_name not in already_bound:
                        bound_tools.append(TOOL_REGISTRY[tool_name])
                        already_bound.add(tool_name)

            # 7. Interaction rules — temperature, max_turns, response_style, language
            interaction_rules = (
                agent_obj.interaction_rules
                if hasattr(agent_obj, "interaction_rules")
                else agent_obj.get("interaction_rules", {})
            ) or {}
            temperature_val = interaction_rules.get("temperature")
            if temperature_val is not None:
                temperature_param = float(temperature_val)
            max_turns_param = int(interaction_rules.get("max_turns", 10))
            response_style = interaction_rules.get("response_style", "balanced")
            language = interaction_rules.get("language", "en")
            if response_style == "concise":
                resolved_system += "\n\nBe concise. Keep responses brief and to the point."
            elif response_style == "verbose":
                resolved_system += "\n\nBe thorough and detailed in your responses."
            if language and language.lower() not in ("en", "english"):
                resolved_system += f"\n\nAlways respond in the following language: {language}."

            log_msg = f"Node '{node_id}' started (model: {resolved_model}"
            log_msg += f", tools: {tool_names})" if tool_names else ")"
            await _write_log("INFO", log_msg, workflow_id, agent_id_str)
        else:
            await _write_log("INFO", f"Node '{node_id}' started (model: {resolved_model})", workflow_id, agent_id_str)

        llm_kwargs: dict = {"model": resolved_model}
        if max_tokens_param:
            llm_kwargs["max_tokens"] = max_tokens_param
        if temperature_param is not None:
            llm_kwargs["temperature"] = temperature_param
        llm = ChatAnthropic(**llm_kwargs)

        messages_in = [SystemMessage(content=resolved_system or "You are a helpful assistant.")]
        messages_in.append(HumanMessage(content=str(user_input)))

        # Guardrail: input rejected — return canned response, skip LLM call
        if _input_rejected is not None:
            text = _input_rejected
            usage_meta: dict = {}
        # ReAct loop when tools are bound; otherwise single-turn
        elif bound_tools:
            from langgraph.prebuilt import create_react_agent
            from langchain_core.messages import AIMessage
            from app.tools.tool_context import tool_config as _tool_config_var
            react_agent = create_react_agent(llm, bound_tools)
            _tc_token = _tool_config_var.set(workflow_tool_config or {})
            try:
                react_result = await react_agent.ainvoke(
                    {"messages": messages_in},
                    config={"recursion_limit": max_turns_param * 2},
                )
            finally:
                _tool_config_var.reset(_tc_token)
            final_msg = next(
                (m for m in reversed(react_result["messages"])
                 if isinstance(m, AIMessage) and not getattr(m, "tool_calls", None)),
                react_result["messages"][-1],
            )
            text = final_msg.content if hasattr(final_msg, "content") else str(final_msg)
            usage_meta = getattr(final_msg, "usage_metadata", None) or {}
        else:
            result = await llm.ainvoke(messages_in)
            text = result.content if hasattr(result, "content") else str(result)
            usage_meta = getattr(result, "usage_metadata", None) or {}

        in_tok = usage_meta.get("input_tokens", 0)
        out_tok = usage_meta.get("output_tokens", 0)
        node_cost = _estimate_cost(resolved_model, in_tok, out_tok)
        node_usage = {"input_tokens": in_tok, "output_tokens": out_tok, "cost_usd": round(node_cost, 6)}

        prev = state.get("usage") or _zero_usage()
        new_usage = {
            "input_tokens": prev["input_tokens"] + in_tok,
            "output_tokens": prev["output_tokens"] + out_tok,
            "total_tokens": prev["input_tokens"] + in_tok + prev["output_tokens"] + out_tok,
            "estimated_cost_usd": round(prev["estimated_cost_usd"] + node_cost, 6),
        }

        await _broadcast(run_id, {
            "event": "node_complete", "node_id": node_id,
            "output": {"text": text}, "usage": node_usage, "ts": _now_iso(),
        })
        await _write_log(
            "INFO",
            f"Node '{node_id}' completed — ↑{in_tok} ↓{out_tok} tokens, ${node_cost:.4f}",
            workflow_id,
            agent_id_str,
        )

        # Persist message roles for this node (fire-and-forget).
        # Effective log level gates which roles are written:
        #   MINIMAL  → role=agent only
        #   STANDARD → role=user + role=agent
        #   VERBOSE  → role=system + role=user + role=assistant/tool + role=agent
        if agent_id_str:
            try:
                from app.database import AsyncSessionLocal
                from app.models.message import Message as MessageModel
                _eff_level = _effective_log_level(agent_obj)
                _eff_rank = _LEVEL_RANK[_eff_level]
                async with AsyncSessionLocal() as _db:
                    # 1. system — VERBOSE only
                    if _eff_rank >= _LEVEL_RANK["VERBOSE"]:
                        _db.add(MessageModel(
                            agent_id=uuid.UUID(agent_id_str),
                            session_id=uuid.UUID(run_id),
                            role="system",
                            content=resolved_system,
                            destination_type=None,
                            destination_ref=None,
                        ))
                    # 2. user — STANDARD and above
                    if _eff_rank >= _LEVEL_RANK["STANDARD"]:
                        _db.add(MessageModel(
                            agent_id=uuid.UUID(agent_id_str),
                            session_id=uuid.UUID(run_id),
                            role="user",
                            content=str(user_input),
                            destination_type=None,
                            destination_ref=None,
                        ))
                    # 3. assistant/tool — VERBOSE only
                    if _eff_rank >= _LEVEL_RANK["VERBOSE"]:
                        if bound_tools and _input_rejected is None:
                            from langchain_core.messages import ToolMessage
                            for _tm in react_result.get("messages", []):
                                if isinstance(_tm, ToolMessage):
                                    _db.add(MessageModel(
                                        agent_id=uuid.UUID(agent_id_str),
                                        session_id=uuid.UUID(run_id),
                                        role="tool",
                                        content=str(_tm.content),
                                        destination_type="agent",
                                        destination_ref=None,
                                    ))
                        else:
                            if _input_rejected is None:
                                _db.add(MessageModel(
                                    agent_id=uuid.UUID(agent_id_str),
                                    session_id=uuid.UUID(run_id),
                                    role="assistant",
                                    content=text,
                                    destination_type="agent",
                                    destination_ref=None,
                                ))
                    # 4. agent — always persisted (minimum at every level)
                    if next_node_type == "agent" and next_agent_id:
                        _agent_dest_type = "agent"
                        _agent_dest_ref = next_agent_id
                    else:
                        _agent_dest_type = "display"
                        _agent_dest_ref = None
                    _db.add(MessageModel(
                        agent_id=uuid.UUID(agent_id_str),
                        session_id=uuid.UUID(run_id),
                        role="agent",
                        content=text,
                        destination_type=_agent_dest_type,
                        destination_ref=_agent_dest_ref,
                    ))
                    await _db.commit()
            except Exception as _exc:
                logger.warning("Could not persist messages for node '%s': %s", node_id, _exc)

        new_messages = list(history) + [text]
        loop_count = state.get("loop_count", 0) + 1
        condition_result = True if loop_count >= max_loops else state.get("condition_result", False)
        return {
            **state,
            "messages": new_messages,
            "current_output": text,
            "loop_count": loop_count,
            "condition_result": condition_result,
            "usage": new_usage,
        }

    return agent_node


_TOOL_NODE_SKIP_KEYS = {"id", "type", "tool_name", "label", "x", "y"}


def _make_tool_node(
    node_id: str,
    tool_name: str,
    run_id: str,
    workflow_id: str | None = None,
    node_params: dict | None = None,
    workflow_tool_config: dict | None = None,
):
    async def tool_node(state: dict) -> dict:
        await _broadcast(run_id, {"event": "node_enter", "node_id": node_id, "ts": _now_iso()})
        await _write_log("INFO", f"Tool '{tool_name}' started", workflow_id)

        from app.tools import PIPELINE_TOOLS
        tool_fn = PIPELINE_TOOLS.get(tool_name)
        if not tool_fn:
            raise ValueError(f"Unknown pipeline tool: '{tool_name}'")

        # Merge order: workflow tool_config (baseline) → node params → state (wins)
        tool_cfg = (workflow_tool_config or {}).get(tool_name, {})
        merged = {**tool_cfg, **(node_params or {}), **state}
        new_state = await tool_fn(merged)

        await _broadcast(run_id, {
            "event": "node_complete",
            "node_id": node_id,
            "output": {"tool": tool_name},
            "ts": _now_iso(),
        })
        await _write_log("INFO", f"Tool '{tool_name}' completed", workflow_id)
        return new_state

    return tool_node


def _make_condition_node(node_id: str, run_id: str):
    async def condition_node(state: dict) -> dict:
        await _broadcast(run_id, {"event": "node_enter", "node_id": node_id, "ts": _now_iso()})
        result = bool(state.get("condition_result", False))
        await _broadcast(run_id, {"event": "node_complete", "node_id": node_id,
                                   "output": {"condition_result": result}, "ts": _now_iso()})
        return state

    return condition_node


def _condition_router(node_id: str, true_target: str, false_target: str):
    def router(state: dict) -> str:
        return true_target if bool(state.get("condition_result", False)) else false_target

    return router


# ---------------------------------------------------------------------------
# Human-in-the-loop node factory
# ---------------------------------------------------------------------------

_HITL_TIMEOUT = 86_400  # 24 hours


def _make_human_review_node(node_id: str, run_id: str, workflow_id: str | None, prompt: str):
    """
    Pause the workflow and wait for human input via POST /runs/{run_id}/resume.

    Flow:
      1. Broadcast `human_review_required` with the last agent output as context.
      2. Set run status → `awaiting_review`.
      3. Await an asyncio.Event (registered in app.hitl).
      4. Inject human input into state as a new message.
      5. Set run status → `running` and broadcast `human_review_completed`.
    """
    async def human_review_node(state: dict) -> dict:
        import asyncio as _asyncio
        from app.hitl import register, cleanup
        from app.models.workflow_run import WorkflowRun as _WFRun
        from app.database import AsyncSessionLocal
        from sqlalchemy import select as _sa_select

        await _broadcast(run_id, {"event": "node_enter", "node_id": node_id, "ts": _now_iso()})

        # Surface the last agent output as context for the reviewer
        messages = state.get("messages", [])
        context = messages[-1] if messages else (state.get("current_output") or "")
        review_prompt = prompt or "Please review the output above and provide feedback or approval to continue."

        # Mark run as awaiting review
        async with AsyncSessionLocal() as db:
            run_obj = (
                await db.execute(_sa_select(_WFRun).where(_WFRun.id == uuid.UUID(run_id)))
            ).scalar_one_or_none()
            if run_obj:
                run_obj.status = "awaiting_review"
                await db.commit()

        await _broadcast(run_id, {
            "event": "human_review_required",
            "node_id": node_id,
            "prompt": review_prompt,
            "context": str(context),
            "ts": _now_iso(),
        })

        # Pause until /resume fires the event
        event, holder = register(run_id)
        try:
            await _asyncio.wait_for(event.wait(), timeout=_HITL_TIMEOUT)
        except _asyncio.TimeoutError:
            cleanup(run_id)
            await _broadcast(run_id, {"event": "run_error", "error": f"Human review timed out for node '{node_id}'", "ts": _now_iso()})
            raise RuntimeError(f"Human review timed out after 24 h for node '{node_id}'")
        finally:
            cleanup(run_id)

        human_input = holder[0] if holder else ""

        # Resume run
        async with AsyncSessionLocal() as db:
            run_obj = (
                await db.execute(_sa_select(_WFRun).where(_WFRun.id == uuid.UUID(run_id)))
            ).scalar_one_or_none()
            if run_obj:
                run_obj.status = "running"
                await db.commit()

        await _broadcast(run_id, {
            "event": "human_review_completed",
            "node_id": node_id,
            "human_input": human_input,
            "ts": _now_iso(),
        })
        await _broadcast(run_id, {"event": "node_complete", "node_id": node_id, "output": {}, "ts": _now_iso()})

        new_messages = list(messages) + [human_input]
        return {**state, "messages": new_messages, "current_output": human_input}

    return human_review_node


# ---------------------------------------------------------------------------
# RAG node factory
# ---------------------------------------------------------------------------

def _make_rag_node(node_id: str, run_id: str, workflow_id: str | None, top_k: int = 5):
    """
    Retrieve relevant context from the knowledge base and prepend it to the
    next agent's input. The retrieved context is injected as the last message
    so the following agent node receives it as its user_input.
    """
    async def rag_node(state: dict) -> dict:
        import json as _json
        from app.embeddings import embed
        from app.database import AsyncSessionLocal
        from sqlalchemy import text as _text

        await _broadcast(run_id, {"event": "node_enter", "node_id": node_id, "ts": _now_iso()})

        # Use the last state message as the retrieval query
        messages = state.get("messages", [])
        query = messages[-1] if messages else (state.get("input") or "")

        context_text = ""
        try:
            vectors = await embed([str(query)])
            query_vec = _json.dumps(vectors[0])
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    _text("""
                        SELECT title, content,
                               1 - (embedding <=> CAST(:query AS vector)) AS score
                        FROM knowledge_base
                        ORDER BY embedding <=> CAST(:query AS vector)
                        LIMIT :top_k
                    """),
                    {"query": query_vec, "top_k": max(1, min(int(top_k), 20))},
                )
                rows = result.mappings().all()
            if rows:
                parts = [
                    f"[Source: {r['title']} | relevance: {r['score']:.2f}]\n{r['content']}"
                    for r in rows
                ]
                context_text = "Retrieved context:\n" + "\n---\n".join(parts)
            else:
                context_text = "No relevant context found in the knowledge base."
        except Exception as exc:
            logger.warning("RAG node '%s' retrieval failed: %s", node_id, exc)
            context_text = f"Knowledge base retrieval failed: {exc}"

        await _broadcast(run_id, {
            "event": "node_complete",
            "node_id": node_id,
            "output": {"retrieved_chunks": len(rows) if "rows" in dir() else 0},
            "ts": _now_iso(),
        })
        await _write_log("INFO", f"RAG node '{node_id}' retrieved context", workflow_id)

        # Prepend context to the messages so the next agent sees it
        new_messages = list(messages) + [context_text]
        return {**state, "messages": new_messages, "current_output": context_text}

    return rag_node


# ---------------------------------------------------------------------------
# WorkflowRunner
# ---------------------------------------------------------------------------

class WorkflowRunner:
    @staticmethod
    def compile(
        graph_definition: Dict[str, Any],
        agents_map: Dict[str, Any],
        run_id: str,
        workflow_id: str | None = None,
        tool_config: dict | None = None,
    ) -> StateGraph:
        """
        Compile graph_definition into a LangGraph StateGraph.

        agents_map: dict of agent_id -> Agent ORM object (or dict with system_prompt)
        """
        nodes = graph_definition.get("nodes", [])
        edges = graph_definition.get("edges", [])
        max_loops = int(graph_definition.get("max_loops", MAX_LOOPS))

        # Build id -> node lookup
        node_map = {n["id"]: n for n in nodes}

        # Build edges: from -> list of (to, branch)
        from typing import DefaultDict
        from collections import defaultdict as _defaultdict

        out_edges: DefaultDict[str, list] = _defaultdict(list)
        for e in edges:
            out_edges[e["from"]].append(e)

        # Determine entry point
        entry_id = next((n["id"] for n in nodes if n["type"] == "start"), None)
        if not entry_id:
            raise ValueError("graph_definition has no start node")

        graph = StateGraph(dict)

        # Register nodes
        for node in nodes:
            nid = node["id"]
            ntype = node["type"]
            if ntype == "start":
                graph.add_node(nid, _make_start_node(run_id))
            elif ntype == "end":
                graph.add_node(nid, _make_end_node(run_id))
            elif ntype == "tool":
                tool_name = node.get("tool_name", "")
                node_params = {k: v for k, v in node.items() if k not in _TOOL_NODE_SKIP_KEYS}
                graph.add_node(nid, _make_tool_node(nid, tool_name, run_id, workflow_id, node_params, tool_config))
            elif ntype == "agent":
                agent_id = node.get("agent_id")
                agent_obj = agents_map.get(str(agent_id)) if agent_id else None
                # Node-level system_prompt overrides the agent's stored prompt
                system_prompt = node.get("system_prompt") or ""
                model_id = node.get("model") or "claude-haiku-4-5-20251001"
                if agent_obj:
                    if not system_prompt:
                        system_prompt = (
                            agent_obj.system_prompt
                            if hasattr(agent_obj, "system_prompt")
                            else agent_obj.get("system_prompt", "")
                        ) or ""
                    agent_model = (
                        agent_obj.model
                        if hasattr(agent_obj, "model")
                        else agent_obj.get("model", "")
                    ) or ""
                    if agent_model and not node.get("model"):
                        model_id = agent_model
                # Compute next-node type and agent_id for destination routing
                _next_node_type: str | None = None
                _next_agent_id: str | None = None
                _node_out = out_edges.get(nid, [])
                if _node_out:
                    _next_nid = _node_out[0]["to"]
                    _next_node = node_map.get(_next_nid, {})
                    _next_node_type = _next_node.get("type")
                    if _next_node_type == "agent":
                        _next_agent_id = str(_next_node.get("agent_id", "")) or None

                graph.add_node(nid, _make_agent_node(
                    nid, system_prompt, model_id, run_id,
                    workflow_id=workflow_id,
                    agent_id_str=str(agent_id) if agent_id else None,
                    agent_obj=agent_obj,
                    max_loops=max_loops,
                    next_node_type=_next_node_type,
                    next_agent_id=_next_agent_id,
                    workflow_tool_config=tool_config,
                ))
            elif ntype == "condition":
                graph.add_node(nid, _make_condition_node(nid, run_id))
            elif ntype == "human_review":
                review_prompt = node.get("prompt", "")
                graph.add_node(nid, _make_human_review_node(nid, run_id, workflow_id, review_prompt))
            elif ntype == "rag":
                top_k = int(node.get("top_k", 5))
                graph.add_node(nid, _make_rag_node(nid, run_id, workflow_id, top_k))

        # Register edges
        for node in nodes:
            nid = node["id"]
            ntype = node["type"]
            node_out = out_edges.get(nid, [])

            if ntype == "end" or not node_out:
                # Terminal — connect to LangGraph END if no outgoing edges
                if ntype == "end":
                    graph.add_edge(nid, END)
                continue

            if ntype == "condition":
                # Find true and false targets
                true_edge = next((e for e in node_out if e.get("branch") == "true"), None)
                false_edge = next((e for e in node_out if e.get("branch") == "false"), None)
                true_target = true_edge["to"] if true_edge else END
                false_target = false_edge["to"] if false_edge else END
                # Map targets: "end" node id -> END if the target is actually an end node
                def _resolve(target):
                    if target != END and node_map.get(target, {}).get("type") == "end":
                        return target  # keep as node id; the end node itself connects to END
                    return target

                graph.add_conditional_edges(
                    nid,
                    _condition_router(nid, _resolve(true_target), _resolve(false_target)),
                )
            else:
                # Normal node: take first outgoing edge (simple linear flow)
                # For multiple outgoing edges without branches, add each
                for e in node_out:
                    graph.add_edge(nid, e["to"])

        graph.set_entry_point(entry_id)
        return graph


async def run_workflow(
    run_id: str,
    workflow_id: str,
    graph_definition: Dict[str, Any],
    agents_map: Dict[str, Any],
    input_data: Dict[str, Any],
    db: AsyncSession,
    tool_config: dict | None = None,
) -> None:
    """
    Execute the compiled workflow graph. Updates WorkflowRun record on
    completion or failure. Broadcasts WebSocket events throughout.
    """
    from app.models.workflow_run import WorkflowRun
    from sqlalchemy import select

    async def _update_run(**kwargs):
        run_obj = (
            await db.execute(
                select(WorkflowRun).where(WorkflowRun.id == uuid.UUID(run_id))
            )
        ).scalar_one_or_none()
        if run_obj:
            for k, v in kwargs.items():
                setattr(run_obj, k, v)
            await db.commit()

    try:
        await _update_run(
            status="running",
            started_at=datetime.now(timezone.utc),
        )
        await _write_log("INFO", f"Workflow run {run_id} started", workflow_id)

        max_loops = int(graph_definition.get("max_loops", MAX_LOOPS))
        compiled = WorkflowRunner.compile(graph_definition, agents_map, run_id, workflow_id, tool_config)
        app_graph = compiled.compile()

        initial_state = {
            "messages": [],
            "current_output": None,
            "condition_result": input_data.get("condition_result", False),
            "loop_count": 0,
            "run_id": run_id,
            "usage": _zero_usage(),
        }

        langsmith_cfg: dict = {}
        if os.environ.get("LANGCHAIN_TRACING_V2") == "true":
            langsmith_cfg = {
                "run_name": f"wf-{(workflow_id or '')[:8]}-run-{run_id[:8]}",
                "tags":     ["vageesh", "workflow-run"],
                "metadata": {"workflow_id": workflow_id or "", "run_id": run_id},
            }

        final_state = await app_graph.ainvoke(
            initial_state,
            config={"recursion_limit": max_loops * 10 + 20, **langsmith_cfg},
        )
        final_usage = final_state.get("usage") or _zero_usage()
        output = {
            "messages": final_state.get("messages", []),
            "current_output": final_state.get("current_output"),
            "usage": final_usage,
        }

        await _update_run(
            status="completed",
            output=output,
            usage=final_usage,
            finished_at=datetime.now(timezone.utc),
        )
        await _broadcast(
            run_id,
            {"event": "run_complete", "status": "completed", "output": output, "usage": final_usage, "ts": _now_iso()},
        )
        await _write_log(
            "INFO",
            f"Workflow run {run_id} completed — "
            f"{final_usage['total_tokens']} tokens, ${final_usage['estimated_cost_usd']:.4f}",
            workflow_id,
        )

    except Exception as exc:
        error_msg = str(exc)
        await _write_log("ERROR", f"Workflow run {run_id} failed: {error_msg}", workflow_id)
        try:
            await _update_run(
                status="failed",
                error=error_msg,
                finished_at=datetime.now(timezone.utc),
            )
        except Exception as db_exc:
            logger.error("Could not mark run %s as failed in DB: %s", run_id, db_exc)
        await _broadcast(run_id, {"event": "run_error", "error": error_msg, "ts": _now_iso()})
