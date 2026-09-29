"""
Integration tests for /api/v1/templates
"""
import pytest


class TestListTemplates:
    async def test_returns_list(self, client):
        res = await client.get("/api/v1/templates",
                               headers={"Authorization": "Bearer test"})
        assert res.status_code == 200
        data = res.json()
        assert isinstance(data, list)
        assert len(data) >= 2  # data-ingestion-pipeline + sre-job-summary

    async def test_template_fields(self, client):
        res = await client.get("/api/v1/templates",
                               headers={"Authorization": "Bearer test"})
        for tmpl in res.json():
            assert "id" in tmpl
            assert "name" in tmpl
            assert "description" in tmpl
            assert "trigger_type" in tmpl

    async def test_known_templates_present(self, client):
        res = await client.get("/api/v1/templates",
                               headers={"Authorization": "Bearer test"})
        ids = [t["id"] for t in res.json()]
        assert "data-ingestion-pipeline" in ids
        assert "sre-job-summary" in ids


class TestInstantiateTemplate:
    async def test_instantiate_data_ingestion(self, client):
        """data-ingestion-pipeline uses legacy agent_config (singular)."""
        res = await client.post(
            "/api/v1/templates/data-ingestion-pipeline/instantiate",
            headers={"Authorization": "Bearer test"},
        )
        assert res.status_code == 201
        data = res.json()
        assert data["name"].startswith("Data Ingestion Pipeline (")
        assert data["name"].endswith(")")
        # suffix must match the first 8 hex chars of the workflow id
        suffix = data["name"].removeprefix("Data Ingestion Pipeline (").removesuffix(")")
        assert data["id"].startswith(suffix)
        assert data["status"] == "draft"
        assert data["graph_definition"] is not None

        # Agent node should have been patched with a real agent_id
        nodes = data["graph_definition"]["nodes"]
        agent_nodes = [n for n in nodes if n["type"] == "agent"]
        assert len(agent_nodes) == 1
        assert agent_nodes[0]["agent_id"] is not None

    async def test_instantiate_sre_job_summary(self, client):
        """sre-job-summary uses agent_configs (list) — creates two agents."""
        res = await client.post(
            "/api/v1/templates/sre-job-summary/instantiate",
            headers={"Authorization": "Bearer test"},
        )
        assert res.status_code == 201
        data = res.json()
        assert "SRE Job Summary" in data["name"]

        # Both agent nodes should be patched with distinct agent IDs
        nodes = data["graph_definition"]["nodes"]
        agent_nodes = [n for n in nodes if n["type"] == "agent"]
        assert len(agent_nodes) == 2
        ids = [n["agent_id"] for n in agent_nodes]
        assert ids[0] is not None
        assert ids[1] is not None
        assert ids[0] != ids[1]

    async def test_instantiate_nonexistent_template(self, client):
        res = await client.post(
            "/api/v1/templates/nonexistent-template/instantiate",
            headers={"Authorization": "Bearer test"},
        )
        assert res.status_code == 404
        assert "not found" in res.json()["detail"].lower()

    async def test_each_instantiation_creates_new_workflow(self, client):
        """Calling instantiate twice creates two separate workflows."""
        r1 = await client.post(
            "/api/v1/templates/sre-job-summary/instantiate",
            headers={"Authorization": "Bearer test"},
        )
        r2 = await client.post(
            "/api/v1/templates/sre-job-summary/instantiate",
            headers={"Authorization": "Bearer test"},
        )
        assert r1.status_code == 201
        assert r2.status_code == 201
        assert r1.json()["id"] != r2.json()["id"]
        assert r1.json()["name"] != r2.json()["name"]

    async def test_instantiate_data_ingestion_has_tool_config(self, client):
        """Instantiated workflow should have tool_config pre-populated from template defaults."""
        res = await client.post(
            "/api/v1/templates/data-ingestion-pipeline/instantiate",
            headers={"Authorization": "Bearer test"},
        )
        data = res.json()
        assert "tool_config" in data
        tc = data["tool_config"]
        assert "scan_csv" in tc
        assert "dataset_dir" in tc["scan_csv"]
        assert "publish_report" in tc
        assert tc["publish_report"]["slack_channel"] == "data-reports"

    async def test_instantiate_portfolio_reco_has_tool_config(self, client):
        res = await client.post(
            "/api/v1/templates/portfolio-recommendation/instantiate",
            headers={"Authorization": "Bearer test"},
        )
        data = res.json()
        tc = data["tool_config"]
        assert tc["publish_rm_alert"]["slack_channel"] == "portfolio-reco"
        assert "product_universe_filter" in tc

    async def test_instantiated_workflow_visible_in_list(self, client):
        await client.post(
            "/api/v1/templates/data-ingestion-pipeline/instantiate",
            headers={"Authorization": "Bearer test"},
        )
        res = await client.get("/api/v1/workflows",
                               headers={"Authorization": "Bearer test"})
        assert res.json()["total"] >= 1
