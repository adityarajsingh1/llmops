from llmops import __version__


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert body["instance"] == "test-gateway"
    assert body["uptime_seconds"] >= 0


def test_openapi_docs_are_served(client):
    spec = client.get("/openapi.json").json()
    assert spec["info"]["title"] == "llmops gateway"
    assert "/health" in spec["paths"]
