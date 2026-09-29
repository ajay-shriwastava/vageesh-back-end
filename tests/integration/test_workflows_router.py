"""
Integration tests for /api/v1/workflows
"""
import uuid
import pytest


SIMPLE_GRAPH = {
    "nodes": [
        {"id": "start", "type": "start", "label": "Start", "x": 0,   "y": 0},
        {"id": "end",   "type": "end",   "label": "End",   "x": 100, "y": 0},
    ],
    "edges": [{"id": "e1", "from": "start", "to": "end"}],
}

WF_PAYLOAD = {
    "name": "My Workflow",
    "description": "A test workflow",
    "graph_definition": SIMPLE_GRAPH,
    "trigger_type": "web_ui",
}


async def _make_workflow(client, payload=None):
    payload = payload or WF_PAYLOAD
    res = await client.post("/api/v1/workflows", json=payload,
                            headers={"Authorization": "Bearer test"})
    assert res.status_code == 201
    return res.json()


class TestCreateWorkflow:
    async def test_creates_workflow_returns_201(self, client):
        res = await client.post("/api/v1/workflows", json=WF_PAYLOAD,
                                headers={"Authorization": "Bearer test"})
        assert res.status_code == 201
        data = res.json()
        assert data["name"] == "My Workflow"
        assert data["status"] == "draft"
        assert data["trigger_type"] == "web_ui"
        assert uuid.UUID(data["id"])

    async def test_graph_definition_stored(self, client):
        wf = await _make_workflow(client)
        assert wf["graph_definition"]["nodes"][0]["type"] == "start"
        assert len(wf["graph_definition"]["edges"]) == 1

    async def test_missing_name_returns_422(self, client):
        res = await client.post("/api/v1/workflows", json={"graph_definition": {}},
                                headers={"Authorization": "Bearer test"})
        assert res.status_code == 422

    async def test_defaults_applied(self, client):
        res = await client.post("/api/v1/workflows", json={"name": "minimal"},
                                headers={"Authorization": "Bearer test"})
        assert res.status_code == 201
        data = res.json()
        assert data["status"] == "draft"
        assert data["trigger_type"] == "web_ui"


class TestListWorkflows:
    async def test_empty_list(self, client):
        res = await client.get("/api/v1/workflows", headers={"Authorization": "Bearer test"})
        assert res.status_code == 200
        data = res.json()
        assert data["total"] == 0
        assert data["items"] == []

    async def test_returns_created_workflows(self, client):
        await _make_workflow(client)
        await _make_workflow(client, {**WF_PAYLOAD, "name": "Second"})
        res = await client.get("/api/v1/workflows", headers={"Authorization": "Bearer test"})
        data = res.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2

    async def test_pagination(self, client):
        for i in range(5):
            await _make_workflow(client, {**WF_PAYLOAD, "name": f"WF {i}"})
        res = await client.get("/api/v1/workflows?skip=2&limit=2",
                               headers={"Authorization": "Bearer test"})
        data = res.json()
        assert data["total"] == 5
        assert len(data["items"]) == 2


class TestGetWorkflow:
    async def test_get_existing(self, client):
        wf = await _make_workflow(client)
        res = await client.get(f"/api/v1/workflows/{wf['id']}",
                               headers={"Authorization": "Bearer test"})
        assert res.status_code == 200
        assert res.json()["id"] == wf["id"]
        assert res.json()["name"] == wf["name"]

    async def test_get_nonexistent_returns_404(self, client):
        res = await client.get(f"/api/v1/workflows/{uuid.uuid4()}",
                               headers={"Authorization": "Bearer test"})
        assert res.status_code == 404
        assert "not found" in res.json()["detail"].lower()


class TestUpdateWorkflow:
    async def test_update_name(self, client):
        wf = await _make_workflow(client)
        res = await client.put(f"/api/v1/workflows/{wf['id']}",
                               json={"name": "Renamed"},
                               headers={"Authorization": "Bearer test"})
        assert res.status_code == 200
        assert res.json()["name"] == "Renamed"

    async def test_update_graph_definition(self, client):
        wf = await _make_workflow(client)
        new_graph = {**SIMPLE_GRAPH, "max_loops": 15}
        res = await client.put(f"/api/v1/workflows/{wf['id']}",
                               json={"graph_definition": new_graph},
                               headers={"Authorization": "Bearer test"})
        assert res.status_code == 200
        assert res.json()["graph_definition"]["max_loops"] == 15

    async def test_update_trigger_type(self, client):
        wf = await _make_workflow(client)
        res = await client.put(f"/api/v1/workflows/{wf['id']}",
                               json={"trigger_type": "message"},
                               headers={"Authorization": "Bearer test"})
        assert res.status_code == 200
        assert res.json()["trigger_type"] == "message"

    async def test_update_nonexistent_returns_404(self, client):
        res = await client.put(f"/api/v1/workflows/{uuid.uuid4()}",
                               json={"name": "x"},
                               headers={"Authorization": "Bearer test"})
        assert res.status_code == 404


class TestToolConfigOnWorkflow:
    async def test_tool_config_defaults_to_empty(self, client):
        wf = await _make_workflow(client)
        assert wf["tool_config"] == {}

    async def test_create_workflow_with_tool_config(self, client):
        payload = {**WF_PAYLOAD, "tool_config": {"scan_csv": {"dataset_dir": "/data"}}}
        res = await client.post("/api/v1/workflows", json=payload,
                                headers={"Authorization": "Bearer test"})
        assert res.status_code == 201
        assert res.json()["tool_config"]["scan_csv"]["dataset_dir"] == "/data"

    async def test_update_tool_config(self, client):
        wf = await _make_workflow(client)
        res = await client.put(
            f"/api/v1/workflows/{wf['id']}",
            json={"tool_config": {"publish_report": {"slack_channel": "dev-alerts"}}},
            headers={"Authorization": "Bearer test"},
        )
        assert res.status_code == 200
        assert res.json()["tool_config"]["publish_report"]["slack_channel"] == "dev-alerts"

    async def test_tool_config_persisted_across_get(self, client):
        wf = await _make_workflow(client)
        await client.put(
            f"/api/v1/workflows/{wf['id']}",
            json={"tool_config": {"scan_csv": {"dataset_dir": "/mnt/data"}}},
            headers={"Authorization": "Bearer test"},
        )
        res = await client.get(f"/api/v1/workflows/{wf['id']}",
                               headers={"Authorization": "Bearer test"})
        assert res.json()["tool_config"]["scan_csv"]["dataset_dir"] == "/mnt/data"


class TestDeleteWorkflow:
    async def test_delete_returns_204(self, client):
        wf = await _make_workflow(client)
        res = await client.delete(f"/api/v1/workflows/{wf['id']}",
                                  headers={"Authorization": "Bearer test"})
        assert res.status_code == 204

    async def test_deleted_workflow_not_found(self, client):
        wf = await _make_workflow(client)
        await client.delete(f"/api/v1/workflows/{wf['id']}",
                            headers={"Authorization": "Bearer test"})
        res = await client.get(f"/api/v1/workflows/{wf['id']}",
                               headers={"Authorization": "Bearer test"})
        assert res.status_code == 404

    async def test_delete_nonexistent_returns_404(self, client):
        res = await client.delete(f"/api/v1/workflows/{uuid.uuid4()}",
                                  headers={"Authorization": "Bearer test"})
        assert res.status_code == 404
