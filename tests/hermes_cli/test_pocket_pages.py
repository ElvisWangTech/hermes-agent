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
