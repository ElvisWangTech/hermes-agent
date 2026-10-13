"""Pocket pages stay inside the built directory."""

from hermes_cli.pocket_pages import _asset


def test_asset_refuses_to_leave_the_dist(tmp_path):
    page = tmp_path / "index.html"
    page.write_text("<html></html>", encoding="utf-8")
    picture = tmp_path / "characters" / "a.jpg"
    picture.parent.mkdir()
    picture.write_bytes(b"jpg")

    assert _asset(tmp_path, "characters/a.jpg") == picture.resolve()
    assert _asset(tmp_path, "../secrets") is None
    assert _asset(tmp_path, "characters/../../secrets") is None


def test_client_does_not_serve_admin_entry(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from hermes_cli import pocket_pages

    (tmp_path / "index.html").write_text('<html>window.__POCKET_SESSION_TOKEN__ = ""</html>')
    monkeypatch.setattr(pocket_pages, "pocket_dist_dir", lambda: tmp_path)
    application = FastAPI()
    application.state.auth_required = True
    pocket_pages.mount_pocket(application)
    with TestClient(application) as client:
        for path in ("/pocket/admin", "/pocket/admin/"):
            response = client.get(path)
            assert response.status_code == 404
