"""
Unit tests for Pydantic schema validation — no DB required.
"""
import uuid
import pytest
from pydantic import ValidationError

from app.schemas.agent import (
    AgentCreate,
    AgentUpdate,
    AgentGuardrailsUpdate,
    AgentInteractionRulesUpdate,
    Guardrails,
    InteractionRules,
)
from app.schemas.message import MessageCreate
from app.schemas.log import LogCreate
from app.schemas.workflow import WorkflowCreate, WorkflowUpdate


# ---------------------------------------------------------------------------
# AgentCreate
# ---------------------------------------------------------------------------

class TestAgentCreateSchema:
    def test_minimal_valid(self):
        a = AgentCreate(name="bot")
        assert a.name == "bot"
        assert a.model == "claude-sonnet-4-6"
        assert a.tools == []
        assert a.channels == []
        assert a.memory_enabled is False

    def test_full_valid(self):
        a = AgentCreate(
            name="full-bot",
            description="desc",
            model="claude-haiku-4-5-20251001",
            system_prompt="You are helpful.",
            tools=["scan_csv"],
            channels=["slack"],
            memory_enabled=True,
        )
        assert a.tools == ["scan_csv"]
        assert a.memory_enabled is True

    def test_name_required(self):
        with pytest.raises(ValidationError):
            AgentCreate()

    def test_name_max_length(self):
        with pytest.raises(ValidationError):
            AgentCreate(name="x" * 256)


class TestAgentUpdateSchema:
    def test_all_optional(self):
        u = AgentUpdate()
        assert u.model_dump(exclude_unset=True) == {}

    def test_partial_update(self):
        u = AgentUpdate(name="new-name")
        data = u.model_dump(exclude_unset=True)
        assert data == {"name": "new-name"}


# ---------------------------------------------------------------------------
# MessageCreate
# ---------------------------------------------------------------------------

class TestMessageCreateSchema:
    def test_valid_roles(self):
        agent_id = uuid.uuid4()
        session_id = uuid.uuid4()
        for role in ("user", "assistant", "system", "tool", "agent"):
            m = MessageCreate(agent_id=agent_id, session_id=session_id, role=role, content="hi")
            assert m.role == role

    def test_invalid_role(self):
        with pytest.raises(ValidationError):
            MessageCreate(
                agent_id=uuid.uuid4(),
                session_id=uuid.uuid4(),
                role="unknown",
                content="hi",
            )

    def test_missing_required_fields(self):
        with pytest.raises(ValidationError):
            MessageCreate(role="user", content="hi")  # missing agent_id + session_id


# ---------------------------------------------------------------------------
# LogCreate
# ---------------------------------------------------------------------------

class TestLogCreateSchema:
    def test_valid_levels(self):
        for level in ("DEBUG", "INFO", "WARNING", "ERROR"):
            log = LogCreate(level=level, message="test")
            assert log.level == level

    def test_invalid_level(self):
        with pytest.raises(ValidationError):
            LogCreate(level="TRACE", message="test")

    def test_optional_fields_default_none(self):
        log = LogCreate(level="INFO", message="ok")
        assert log.agent_id is None
        assert log.workflow_id is None
        assert log.metadata is None


# ---------------------------------------------------------------------------
# WorkflowCreate
# ---------------------------------------------------------------------------

class TestWorkflowCreateSchema:
    def test_minimal(self):
        w = WorkflowCreate(name="my-workflow")
        assert w.name == "my-workflow"
        assert w.status == "draft"
        assert w.trigger_type == "web_ui"
        assert w.graph_definition == {}

    def test_with_graph_definition(self):
        gd = {"nodes": [{"id": "start", "type": "start"}], "edges": []}
        w = WorkflowCreate(name="wf", graph_definition=gd)
        assert w.graph_definition["nodes"][0]["type"] == "start"

    def test_name_required(self):
        with pytest.raises(ValidationError):
            WorkflowCreate()

    def test_tool_config_defaults_to_empty(self):
        w = WorkflowCreate(name="wf")
        assert w.tool_config == {}

    def test_tool_config_accepted(self):
        w = WorkflowCreate(name="wf", tool_config={"scan_csv": {"dataset_dir": "/tmp"}})
        assert w.tool_config["scan_csv"]["dataset_dir"] == "/tmp"


class TestWorkflowUpdateSchema:
    def test_tool_config_optional(self):
        u = WorkflowUpdate()
        assert u.tool_config is None

    def test_tool_config_patch(self):
        u = WorkflowUpdate(tool_config={"publish_report": {"slack_channel": "dev"}})
        assert u.tool_config["publish_report"]["slack_channel"] == "dev"


# ---------------------------------------------------------------------------
# InteractionRules validation
# ---------------------------------------------------------------------------

class TestInteractionRulesSchema:
    def test_defaults(self):
        r = InteractionRules()
        assert r.temperature == 0.7
        assert r.max_turns == 10
        assert r.language == "en"

    def test_temperature_bounds(self):
        with pytest.raises(ValidationError):
            InteractionRules(temperature=2.1)
        with pytest.raises(ValidationError):
            InteractionRules(temperature=-0.1)

    def test_max_turns_bounds(self):
        with pytest.raises(ValidationError):
            InteractionRules(max_turns=0)
        with pytest.raises(ValidationError):
            InteractionRules(max_turns=101)


# ---------------------------------------------------------------------------
# Guardrails validation
# ---------------------------------------------------------------------------

class TestGuardrailsSchema:
    def test_defaults(self):
        g = Guardrails()
        assert g.max_tokens_per_response == 2048
        assert g.rate_limit_per_minute == 60
        assert g.restricted_topics == []

    def test_rate_limit_min(self):
        with pytest.raises(ValidationError):
            Guardrails(rate_limit_per_minute=0)

    def test_rate_limit_max(self):
        with pytest.raises(ValidationError):
            Guardrails(rate_limit_per_minute=1001)

    def test_max_tokens_bounds(self):
        with pytest.raises(ValidationError):
            Guardrails(max_tokens_per_response=0)
        with pytest.raises(ValidationError):
            Guardrails(max_tokens_per_response=32769)



