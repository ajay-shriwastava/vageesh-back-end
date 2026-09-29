import asyncio
import logging
import uuid

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.agent import Agent
from app.models.workflow import Workflow
from app.models.workflow_run import WorkflowRun
from app.schemas.workflow_run import WorkflowRunCreate, WorkflowRunListOut, WorkflowRunOut, WorkflowRunResume
from app.ws_manager import manager

# REST routes live under /api/v1/workflows
router = APIRouter(prefix="/api/v1/workflows", tags=["workflow-runs"])

# WebSocket routes live under /ws/workflows — separate router registered in main.py
ws_router = APIRouter(tags=["workflow-runs-ws"])


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/{workflow_id}/run",
    response_model=WorkflowRunOut,
    status_code=status.HTTP_201_CREATED,
)
async def start_run(
    workflow_id: uuid.UUID,
    body: WorkflowRunCreate,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_current_user),
):
    workflow = (
        await db.execute(select(Workflow).where(Workflow.id == workflow_id))
    ).scalar_one_or_none()
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")

    run = WorkflowRun(
        workflow_id=workflow_id,
        status="pending",
        input=body.input or {},
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    # Build agents map from graph_definition agent_ids
    agent_ids = [
        n.get("agent_id")
        for n in workflow.graph_definition.get("nodes", [])
        if n.get("type") == "agent" and n.get("agent_id")
    ]
    agents_map: dict = {}
    if agent_ids:
        rows = (
            await db.execute(
                select(Agent).where(Agent.id.in_([uuid.UUID(str(aid)) for aid in agent_ids]))
            )
        ).scalars().all()
        agents_map = {str(a.id): a for a in rows}

    # Fire-and-forget — execution runs in background
    from app.workflow_runner import run_workflow

    # We need a separate DB session for the background task
    from app.database import AsyncSessionLocal

    async def _background():
        uses_slack = any(
            "slack" in (a.channels or [])
            for a in agents_map.values()
        )
        if uses_slack:
            try:
                from app.slack_bot import ensure_connected
                await ensure_connected()
            except Exception as exc:
                logger.error("Slack reconnect failed — workflow will proceed without Slack: %s", exc)

        async with AsyncSessionLocal() as bg_db:
            await run_workflow(
                run_id=str(run.id),
                workflow_id=str(workflow_id),
                graph_definition=workflow.graph_definition,
                agents_map=agents_map,
                input_data=body.input or {},
                db=bg_db,
                tool_config=workflow.tool_config or {},
            )

    asyncio.create_task(_background())

    return run


@router.get("/{workflow_id}/runs", response_model=WorkflowRunListOut)
async def list_runs(
    workflow_id: uuid.UUID,
    skip: int = 0,
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_current_user),
):
    workflow = (
        await db.execute(select(Workflow).where(Workflow.id == workflow_id))
    ).scalar_one_or_none()
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")

    total = (
        await db.execute(
            select(func.count()).select_from(WorkflowRun).where(WorkflowRun.workflow_id == workflow_id)
        )
    ).scalar_one()
    rows = (
        await db.execute(
            select(WorkflowRun)
            .where(WorkflowRun.workflow_id == workflow_id)
            .order_by(WorkflowRun.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
    ).scalars().all()
    return WorkflowRunListOut(items=list(rows), total=total, skip=skip, limit=limit)


@router.get("/{workflow_id}/runs/{run_id}", response_model=WorkflowRunOut)
async def get_run(
    workflow_id: uuid.UUID,
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_current_user),
):
    run = (
        await db.execute(
            select(WorkflowRun).where(
                WorkflowRun.id == run_id,
                WorkflowRun.workflow_id == workflow_id,
            )
        )
    ).scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


# ---------------------------------------------------------------------------
# Human-in-the-loop resume endpoint
# ---------------------------------------------------------------------------


@router.post(
    "/{workflow_id}/runs/{run_id}/resume",
    response_model=WorkflowRunOut,
)
async def resume_run(
    workflow_id: uuid.UUID,
    run_id: uuid.UUID,
    body: WorkflowRunResume,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_current_user),
):
    run = (
        await db.execute(
            select(WorkflowRun).where(
                WorkflowRun.id == run_id,
                WorkflowRun.workflow_id == workflow_id,
            )
        )
    ).scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status != "awaiting_review":
        raise HTTPException(
            status_code=400,
            detail=f"Run is not awaiting review (current status: {run.status})",
        )

    from app.hitl import resume
    if not resume(str(run_id), body.human_input):
        raise HTTPException(
            status_code=409,
            detail="No pending human review found for this run — it may have timed out",
        )

    return run


# ---------------------------------------------------------------------------
# WebSocket endpoint  (registered on ws_router, mounted at "/" in main.py)
# ---------------------------------------------------------------------------


@ws_router.websocket("/ws/workflows/{workflow_id}/runs/{run_id}")
async def ws_run_events(
    workflow_id: uuid.UUID,
    run_id: uuid.UUID,
    websocket: WebSocket,
    token: str = "dev-token",
):
    # Minimal token check — accept any non-empty token (matches stub auth)
    if not token:
        await websocket.close(code=4001)
        return

    run_id_str = str(run_id)
    await manager.connect(run_id_str, websocket)
    try:
        # Keep alive until client disconnects
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(run_id_str, websocket)
