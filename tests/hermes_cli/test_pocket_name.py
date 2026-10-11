"""The handheld client's name is one memory line, written before any session."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli.web_routers.pocket import name_memory_line, router
from tools.memory_tool import get_memory_dir


def test_name_memory_line_is_the_sentence_the_client_expects():
    assert name_memory_line("  小满  ") == "我的名字是小满，这是用户给我起的。"


def test_name_memory_line_rejects_empty_and_file_breaking_marks():
    for bad in ("", "   ", "a" * 25, "小\n满", "小§满"):
        try:
            name_memory_line(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_post_name_appends_memory_before_a_session_exists():
    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).post("/api/pocket/name", json={"name": "小满"})

    assert response.status_code == 200
    text = (get_memory_dir() / "MEMORY.md").read_text(encoding="utf-8")
    assert "我的名字是小满，这是用户给我起的。" in text
