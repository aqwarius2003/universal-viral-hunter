"""
Tests for modernized modular frontend static files and routes.
"""
import pytest
from pathlib import Path
from aiohttp.test_utils import TestClient, TestServer
from src.web.server import create_app
from src.config import settings


@pytest.mark.asyncio
async def test_frontend_static_files_exist():
    static_dir = Path("src/web/static")
    assert (static_dir / "index.html").is_file()
    assert (static_dir / "index.legacy.html").is_file()
    
    # CSS files
    css_dir = static_dir / "css"
    for css_file in ["variables.css", "base.css", "header.css", "components.css", "cards.css", "video-facade.css", "modals.css"]:
        assert (css_dir / css_file).is_file(), f"Missing CSS file: {css_file}"

    # JS files
    js_dir = static_dir / "js"
    for js_file in ["app.js", "api.js", "auth.js", "toast.js", "video-player.js", "cards.js", "queue.js", "published.js", "modal.js", "topics.js", "settings.js"]:
        assert (js_dir / js_file).is_file(), f"Missing JS file: {js_file}"


@pytest.mark.asyncio
async def test_frontend_index_and_static_serving(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", False)
    app = create_app()
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()

    try:
        # 1. Main index.html
        resp = await client.get("/")
        assert resp.status == 200
        content = await resp.text()
        assert "Viral Hunter" in content
        assert "/static/css/video-facade.css" in content
        assert "/static/js/app.js" in content
        assert "video-facade" in content or "btn-view-grid" in content

        # 2. CSS endpoint
        css_resp = await client.get("/static/css/video-facade.css")
        assert css_resp.status == 200
        css_text = await css_resp.text()
        assert ".video-facade" in css_text
        assert ".video-play-btn" in css_text

        # 3. JS endpoint
        js_resp = await client.get("/static/js/video-player.js")
        assert js_resp.status == 200
        js_text = await js_resp.text()
        assert "renderVideoFacade" in js_text
        assert "activateVideoPlayer" in js_text
    finally:
        await client.close()
