"""
MCP server for Vageesh — exposes agent memory and knowledge base as MCP tools.

Mount:  app.mount("/mcp", mcp_asgi_app)
Auth:   X-MCP-API-Key header (value from MCP_API_KEY env var)

Tools (8):
  Memory:    get_memory, list_memory, set_memory, delete_memory
  Knowledge: list_knowledge, add_knowledge, add_knowledge_file, search_knowledge

Every tool call is persisted to mcp_audit_log (fire-and-forget).
params_summary never stores raw content or file bytes — only metadata.
"""

import asyncio
import base64
import io
import json
import logging
import os
import uuid
from datetime import datetime, timezone

from fastmcp import FastMCP
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import AsyncSessionLocal
from app.embeddings import chunk_text, embed
from app.models.mcp_audit_log import McpAuditLog
from app.models.memory import AgentMemory

logger = logging.getLogger(__name__)

mcp = FastMCP("Vageesh")


# ---------------------------------------------------------------------------
# Auth helper — called at the top of every tool
# ---------------------------------------------------------------------------

def _get_api_key() -> str | None:
    return os.environ.get("MCP_API_KEY", "").strip() or None


# ---------------------------------------------------------------------------
# Audit log (fire-and-forget)
# ---------------------------------------------------------------------------

async def _audit(
    caller_id: str,
    tool_name: str,
    params_summary: dict,
    result_summary: str | None,
    agent_id: str | None = None,
) -> None:
    try:
        async with AsyncSessionLocal() as db:
            entry = McpAuditLog(
                id=uuid.uuid4(),
                caller_id=caller_id,
                tool_name=tool_name,
                agent_id=uuid.UUID(agent_id) if agent_id else None,
                params_summary=params_summary,
                result_summary=result_summary,
            )
            db.add(entry)
            await db.commit()
    except Exception as exc:
        logger.warning("mcp_audit_log write failed: %s", exc)


def _fire_audit(
    caller_id: str,
    tool_name: str,
    params_summary: dict,
    result_summary: str | None,
    agent_id: str | None = None,
) -> None:
    asyncio.create_task(
        _audit(caller_id, tool_name, params_summary, result_summary, agent_id)
    )


# ---------------------------------------------------------------------------
# Memory tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_memory(agent_id: str, key: str, caller_id: str) -> str:
    """Retrieve a single memory entry for an agent by key."""
    api_key = _get_api_key()
    if not api_key:
        return json.dumps({"error": "MCP server not configured"})

    if len(key) > 255:
        return json.dumps({"error": "key exceeds 255 characters"})

    try:
        agent_uuid = uuid.UUID(agent_id)
    except ValueError:
        return json.dumps({"error": "invalid agent_id"})

    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(AgentMemory).where(
                    AgentMemory.agent_id == agent_uuid,
                    AgentMemory.key == key,
                )
            )
        ).scalar_one_or_none()

    if not row:
        result = {"error": f"key '{key}' not found"}
        result_summary = "not found"
    else:
        result = {
            "key": row.key,
            "value": row.value,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
        result_summary = "found"

    _fire_audit(caller_id, "get_memory", {"agent_id": agent_id, "key": key}, result_summary, agent_id)
    return json.dumps(result)


@mcp.tool()
async def list_memory(agent_id: str, caller_id: str) -> str:
    """List all memory entries for an agent."""
    api_key = _get_api_key()
    if not api_key:
        return json.dumps({"error": "MCP server not configured"})

    try:
        agent_uuid = uuid.UUID(agent_id)
    except ValueError:
        return json.dumps({"error": "invalid agent_id"})

    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(AgentMemory)
                .where(AgentMemory.agent_id == agent_uuid)
                .order_by(AgentMemory.key)
            )
        ).scalars().all()

    result = [
        {
            "key": r.key,
            "value": r.value,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    ]
    _fire_audit(caller_id, "list_memory", {"agent_id": agent_id}, f"{len(result)} entries", agent_id)
    return json.dumps(result)


@mcp.tool()
async def set_memory(agent_id: str, key: str, value: str, caller_id: str) -> str:
    """Upsert a memory key-value pair for an agent."""
    api_key = _get_api_key()
    if not api_key:
        return json.dumps({"error": "MCP server not configured"})

    if len(key) > 255:
        return json.dumps({"error": "key exceeds 255 characters"})
    if len(value) > 10_000:
        return json.dumps({"error": "value exceeds 10,000 characters"})

    try:
        agent_uuid = uuid.UUID(agent_id)
    except ValueError:
        return json.dumps({"error": "invalid agent_id"})

    async with AsyncSessionLocal() as db:
        stmt = (
            pg_insert(AgentMemory)
            .values(
                id=uuid.uuid4(),
                agent_id=agent_uuid,
                key=key,
                value=value,
            )
            .on_conflict_do_update(
                constraint="uq_agent_memory_agent_key",
                set_={"value": value, "updated_at": func.now()},
            )
        )
        await db.execute(stmt)
        await db.commit()

    result = {"key": key, "value": value}
    _fire_audit(caller_id, "set_memory", {"agent_id": agent_id, "key": key, "value_length": len(value)}, "upserted", agent_id)
    return json.dumps(result)


@mcp.tool()
async def delete_memory(agent_id: str, key: str, caller_id: str) -> str:
    """Delete a memory entry for an agent by key."""
    api_key = _get_api_key()
    if not api_key:
        return json.dumps({"error": "MCP server not configured"})

    try:
        agent_uuid = uuid.UUID(agent_id)
    except ValueError:
        return json.dumps({"error": "invalid agent_id"})

    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(AgentMemory).where(
                    AgentMemory.agent_id == agent_uuid,
                    AgentMemory.key == key,
                )
            )
        ).scalar_one_or_none()

        if not row:
            result = {"error": f"key '{key}' not found"}
            result_summary = "not found"
        else:
            await db.delete(row)
            await db.commit()
            result = {"deleted": True}
            result_summary = "deleted"

    _fire_audit(caller_id, "delete_memory", {"agent_id": agent_id, "key": key}, result_summary, agent_id)
    return json.dumps(result)


# ---------------------------------------------------------------------------
# Knowledge tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_knowledge(caller_id: str, skip: int = 0, limit: int = 50) -> str:
    """List knowledge base entries (paginated)."""
    api_key = _get_api_key()
    if not api_key:
        return json.dumps({"error": "MCP server not configured"})

    limit = max(1, min(limit, 200))

    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT id, title, created_at FROM knowledge_base "
                    "ORDER BY created_at DESC OFFSET :skip LIMIT :limit"
                ),
                {"skip": skip, "limit": limit},
            )
        ).mappings().all()

    result = [
        {
            "id": str(r["id"]),
            "title": r["title"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]
    _fire_audit(caller_id, "list_knowledge", {"skip": skip, "limit": limit}, f"{len(result)} entries")
    return json.dumps(result)


@mcp.tool()
async def add_knowledge(title: str, content: str, caller_id: str) -> str:
    """Ingest a text document into the knowledge base."""
    api_key = _get_api_key()
    if not api_key:
        return json.dumps({"error": "MCP server not configured"})

    if len(content) > 500_000:
        return json.dumps({"error": "content exceeds 500,000 characters"})

    chunks = chunk_text(content)
    vectors = await embed(chunks)

    async with AsyncSessionLocal() as db:
        for chunk, vector in zip(chunks, vectors):
            entry_id = uuid.uuid4()
            await db.execute(
                text(
                    "INSERT INTO knowledge_base (id, title, content, embedding, metadata_, created_at) "
                    "VALUES (:id, :title, :content, CAST(:embedding AS vector), CAST(:metadata AS jsonb), NOW())"
                ),
                {
                    "id": str(entry_id),
                    "title": title,
                    "content": chunk,
                    "embedding": json.dumps(vector),
                    "metadata": json.dumps({}),
                },
            )
        await db.commit()

    result = {"chunks_created": len(chunks), "title": title}
    _fire_audit(
        caller_id, "add_knowledge",
        {"title": title, "content_length": len(content)},
        f"{len(chunks)} chunks created",
    )
    return json.dumps(result)


@mcp.tool()
async def add_knowledge_file(title: str, content_base64: str, file_type: str, caller_id: str) -> str:
    """Ingest a PDF or TXT file (base64-encoded) into the knowledge base."""
    api_key = _get_api_key()
    if not api_key:
        return json.dumps({"error": "MCP server not configured"})

    if file_type not in ("pdf", "txt"):
        return json.dumps({"error": "file_type must be 'pdf' or 'txt'"})

    try:
        raw_bytes = base64.b64decode(content_base64)
    except Exception:
        return json.dumps({"error": "invalid base64 encoding"})

    try:
        if file_type == "pdf":
            from pypdf import PdfReader  # local import — optional dep

            def _extract(data: bytes) -> str:
                reader = PdfReader(io.BytesIO(data))
                return "\n".join(page.extract_text() or "" for page in reader.pages).strip()

            text_content = await asyncio.to_thread(_extract, raw_bytes)
        else:
            text_content = raw_bytes.decode("utf-8").strip()
    except Exception as exc:
        logger.error("add_knowledge_file parse error: %s", exc)
        return json.dumps({"error": "failed to process file"})

    if not text_content:
        return json.dumps({"error": "no text could be extracted from the file"})

    chunks = chunk_text(text_content)
    vectors = await embed(chunks)

    async with AsyncSessionLocal() as db:
        for chunk, vector in zip(chunks, vectors):
            entry_id = uuid.uuid4()
            await db.execute(
                text(
                    "INSERT INTO knowledge_base (id, title, content, embedding, metadata_, created_at) "
                    "VALUES (:id, :title, :content, CAST(:embedding AS vector), CAST(:metadata AS jsonb), NOW())"
                ),
                {
                    "id": str(entry_id),
                    "title": title,
                    "content": chunk,
                    "embedding": json.dumps(vector),
                    "metadata": json.dumps({"file_type": file_type}),
                },
            )
        await db.commit()

    result = {"chunks_created": len(chunks), "title": title}
    _fire_audit(
        caller_id, "add_knowledge_file",
        {"title": title, "file_type": file_type, "encoded_length": len(content_base64)},
        f"{len(chunks)} chunks created",
    )
    return json.dumps(result)


@mcp.tool()
async def search_knowledge(query: str, caller_id: str, top_k: int = 3) -> str:
    """Semantic similarity search over the knowledge base."""
    api_key = _get_api_key()
    if not api_key:
        return json.dumps({"error": "MCP server not configured"})

    top_k = max(1, min(top_k, 20))

    vectors = await embed([query])
    query_vec = json.dumps(vectors[0])

    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT id, title, content, "
                    "1 - (embedding <=> CAST(:query AS vector)) AS score "
                    "FROM knowledge_base "
                    "ORDER BY embedding <=> CAST(:query AS vector) "
                    "LIMIT :top_k"
                ),
                {"query": query_vec, "top_k": top_k},
            )
        ).mappings().all()

    result = [
        {
            "id": str(r["id"]),
            "title": r["title"],
            "content": r["content"],
            "score": float(r["score"]),
        }
        for r in rows
    ]
    _fire_audit(
        caller_id, "search_knowledge",
        {"query_length": len(query), "top_k": top_k},
        f"{len(result)} results",
    )
    return json.dumps(result)


# ---------------------------------------------------------------------------
# ASGI app with X-MCP-API-Key authentication middleware
# ---------------------------------------------------------------------------

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


class McpAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        api_key = _get_api_key()
        if api_key is None:
            return JSONResponse({"error": "MCP server not configured"}, status_code=503)
        provided = request.headers.get("x-mcp-api-key", "")
        if provided != api_key:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await call_next(request)


# Build the ASGI app to mount into FastAPI
_base_asgi = mcp.http_app(path="/")

from starlette.applications import Starlette
from starlette.routing import Mount

mcp_asgi_app = Starlette(
    routes=[Mount("/", app=_base_asgi)],
    middleware=[
        # Starlette-style middleware tuple
    ],
)

# Apply auth middleware directly on the base app wrapper
from starlette.middleware import Middleware

mcp_asgi_app = McpAuthMiddleware(mcp.http_app(path="/"))
