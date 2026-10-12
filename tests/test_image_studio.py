import base64

from fastapi.testclient import TestClient

from app import main


PNG = b"\x89PNG\r\n\x1a\nstandalone-image"


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"data": [{"b64_json": base64.b64encode(PNG).decode()}]}


class FakeClient:
    requests = []

    def __init__(self, **_kwargs):
        self.request = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, endpoint, headers, json):
        self.request = (endpoint, headers, json)
        self.requests.append(self.request)
        return FakeResponse()


def test_workspace_has_accessible_standalone_image_tab():
    source = TestClient(main.app).get("/").text

    assert 'role="tablist" aria-label="Production tools"' in source
    assert 'id="podcast-tab"' in source
    assert 'id="image-tab"' in source
    assert 'id="image-panel"' in source
    assert "fetch('/api/images/generations'" in source


def test_standalone_image_generation_returns_png(monkeypatch):
    FakeClient.requests.clear()
    monkeypatch.setattr(main, "CLIP_IMAGE_PROVIDER", "local")
    monkeypatch.setattr(main.httpx, "AsyncClient", FakeClient)

    response = TestClient(main.app).post(
        "/api/images/generations",
        data={"prompt": "Amber lighthouse in a storm", "aspect": "landscape"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == PNG
    endpoint, _headers, payload = FakeClient.requests[-1]
    assert endpoint == main.LOCAL_IMAGES_URL
    assert payload["prompt"] == "Amber lighthouse in a storm"
    assert payload["size"] == "1536x1024"


def test_standalone_image_generation_validates_input(monkeypatch):
    monkeypatch.setattr(main, "CLIP_IMAGE_PROVIDER", "local")
    client = TestClient(main.app)

    assert client.post("/api/images/generations", data={"prompt": "   "}).status_code == 400
    assert client.post(
        "/api/images/generations", data={"prompt": "valid", "aspect": "cinema"}
    ).status_code == 400


def test_hosted_quality_is_configurable_and_studio_can_override(monkeypatch):
    monkeypatch.setattr(main, "CLIP_IMAGE_PROVIDER", "openai")
    monkeypatch.setattr(main, "OPENAI_API_KEY", "test-only")
    monkeypatch.setattr(main, "OPENAI_IMAGE_QUALITY", "high")
    monkeypatch.setattr(main.httpx, "AsyncClient", FakeClient)
    client = TestClient(main.app)
    for fields, expected in [({}, "high"), ({"quality": "medium"}, "medium"), ({"quality": "low"}, "low")]:
        result = client.post('/api/images/generations', data={"prompt": "A photographic portrait", **fields})
        assert result.status_code == 200
        endpoint, headers, payload = FakeClient.requests[-1]
        assert endpoint == main.OPENAI_IMAGES_URL
        assert headers["Authorization"] == "Bearer test-only"
        assert payload["quality"] == expected
        assert "preset" not in payload and "seed" not in payload
    assert client.post('/api/images/generations', data={"prompt": "Portrait", "quality": "invalid"}).status_code == 400


def test_local_studio_explicitly_selects_draft_or_detail(monkeypatch):
    monkeypatch.setattr(main, "CLIP_IMAGE_PROVIDER", "local")
    monkeypatch.setattr(main.httpx, "AsyncClient", FakeClient)
    client = TestClient(main.app)
    for preset, model in [("draft", main.LOCAL_IMAGE_MODEL), ("detail", main.LOCAL_IMAGE_DETAIL_MODEL)]:
        assert client.post('/api/images/generations', data={"prompt": "Portrait", "preset": preset}).status_code == 200
        payload = FakeClient.requests[-1][2]
        assert payload["preset"] == preset and payload["model"] == model
        assert "quality" not in payload
    assert client.post('/api/images/generations', data={"prompt": "Portrait", "preset": "invalid"}).status_code == 400
