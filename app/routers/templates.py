"""Templates — list pre-built workflow templates and instantiate them."""

import copy
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.agent import Agent
from app.models.workflow import Workflow
from app.schemas.workflow import WorkflowOut
from app.templates import TEMPLATES, TEMPLATES_BY_ID

router = APIRouter(prefix="/api/v1/templates", tags=["templates"])


@router.get("")
async def list_templates(_: dict = Depends(get_current_user)):
    """Return all available workflow templates (metadata only, no instantiation)."""
    return [
        {
            "id":           t["id"],
            "name":         t["name"],
            "description":  t["description"],
            "trigger_type": t.get("trigger_type", "web_ui"),
        }
        for t in TEMPLATES
    ]


@router.post("/{template_id}/instantiate", response_model=WorkflowOut, status_code=status.HTTP_201_CREATED)
async def instantiate_template(
    template_id: str,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_current_user),
):
    """Create a new Workflow (and its agent, if configured) from a template."""
    tmpl = TEMPLATES_BY_ID.get(template_id)
    if not tmpl:
        raise HTTPException(status_code=404, detail=f"Template '{template_id}' not found")

    # Pre-generate the workflow UUID so agents and workflow share the same suffix,
    # making the suffix directly traceable to the workflow's real DB id.
    wf_id = uuid.uuid4()
    suffix = f"({wf_id.hex[:8]})"

    # Deep-copy graph so we can patch agent_id without mutating the template
    graph_def = copy.deepcopy(tmpl["graph_definition"])

    # agent_configs (list) — each entry targets a specific node_id
    for agent_cfg in tmpl.get("agent_configs", []):
        agent = Agent(
            id=uuid.uuid4(),
            name=f"{agent_cfg['name']} {suffix}",
            description=agent_cfg.get("description"),
            model=agent_cfg.get("model", "claude-haiku-4-5-20251001"),
            system_prompt=agent_cfg.get("system_prompt"),
            tools=agent_cfg.get("tools", []),
            channels=agent_cfg.get("channels", []),
            memory_enabled=agent_cfg.get("memory_enabled", False),
        )
        db.add(agent)
        await db.flush()
        target_node_id = agent_cfg.get("node_id")
        for node in graph_def.get("nodes", []):
            if node.get("id") == target_node_id:
                node["agent_id"] = str(agent.id)

    # agent_config (singular, legacy) — patches all agent nodes with agent_id=None
    agent_cfg = tmpl.get("agent_config")
    if agent_cfg:
        agent = Agent(
            id=uuid.uuid4(),
            name=f"{agent_cfg['name']} {suffix}",
            description=agent_cfg.get("description"),
            model=agent_cfg.get("model", "claude-haiku-4-5-20251001"),
            system_prompt=agent_cfg.get("system_prompt"),
            tools=agent_cfg.get("tools", []),
            channels=agent_cfg.get("channels", []),
            memory_enabled=agent_cfg.get("memory_enabled", False),
        )
        db.add(agent)
        await db.flush()
        for node in graph_def.get("nodes", []):
            if node.get("type") == "agent" and node.get("agent_id") is None:
                node["agent_id"] = str(agent.id)

    workflow = Workflow(
        id=wf_id,
        name=f"{tmpl['name']} {suffix}",
        description=tmpl["description"],
        status="draft",
        graph_definition=graph_def,
        trigger_type=tmpl.get("trigger_type", "web_ui"),
        tool_config=tmpl.get("tool_config_defaults", {}),
    )
    db.add(workflow)
    await db.commit()
    await db.refresh(workflow)

    return workflow
