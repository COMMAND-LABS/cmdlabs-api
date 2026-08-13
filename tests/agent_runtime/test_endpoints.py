"""Integration tests for API endpoints.

Uses FastAPI's TestClient with overridden dependencies — no real DB or LLM.
"""


def test_healthcheck(test_client):
    client, _ = test_client
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json() == {"status": "OK!"}


# The standalone agent-api's GET /api/agents/{id} tests were dropped in the
# merge: that duplicate route was removed in favor of the CRUD router's copy,
# which tests/test_agents.py already covers against a real test DB.


def test_docs_endpoint(test_client):
    client, _ = test_client
    resp = client.get("/api/docs")
    assert resp.status_code == 200


def test_stream_requires_body(test_client):
    client, _ = test_client
    resp = client.post("/api/agents/1/stream")
    assert resp.status_code == 422
