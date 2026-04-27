"""Integration tests for the FastAPI surface (no real ASR / LLM calls)."""
from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

import main


client = TestClient(main.app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_metrics_endpoint_exposes_prometheus():
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "smn_pipeline_latency_seconds" in r.text


def test_serve_ui_returns_html():
    r = client.get("/")
    assert r.status_code == 200
    assert "<html" in r.text.lower()


def test_unsupported_extension_rejected():
    r = client.post(
        "/transcribe",
        files={"file": ("note.txt", b"hello", "text/plain")},
    )
    assert r.status_code == 400
    assert "Unsupported file type" in r.text


def test_transcribe_url_dispatch(monkeypatch):
    """POST /transcribe-url returns task_id and the background task can be inspected."""
    def fake_run(task_id, url, tmp_dir, summarize):
        main.tasks[task_id]["status"] = "done"
        main.tasks[task_id]["transcript"] = "stubbed"

    monkeypatch.setattr(main, "run_url_pipeline", fake_run)
    r = client.post("/transcribe-url", json={"url": "https://example.com/a.mp3"})
    assert r.status_code == 200
    task_id = r.json()["task_id"]
    assert task_id


def test_summarize_endpoint_rejects_empty():
    r = client.post("/summarize", json={"transcript": "  "})
    assert r.status_code == 400


def test_summarize_endpoint_invokes_pipeline(monkeypatch):
    async def fake_pipeline(t):
        return {
            "cleaned_transcript": t,
            "summary": ["bullet"],
            "action_items": [],
            "decisions": [],
            "next_steps": [],
        }

    monkeypatch.setattr(main, "summarize_transcript", fake_pipeline)
    r = client.post("/summarize", json={"transcript": "hello world"})
    assert r.status_code == 200
    assert r.json()["summary"] == ["bullet"]
