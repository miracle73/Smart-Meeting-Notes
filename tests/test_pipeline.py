"""Pipeline tests with the LLM mocked at the `_chat_raw` layer.

Verifies:
- Structured outputs are validated against the schema.
- Validation failure triggers a self-repair retry.
- Tenacity retries transient errors.
"""
from __future__ import annotations

import json

import pytest

from agents import pipeline


@pytest.mark.asyncio
async def test_summarize_transcript_happy_path(monkeypatch, sample_transcript):
    calls: list[str] = []

    async def fake_chat_raw(system: str, user: str, agent: str, json_mode: bool = False):
        calls.append(agent)
        if agent == "cleaner":
            return sample_transcript
        if agent == "summarizer":
            return json.dumps({"summary": ["Coverage at 60%, target 80%."]})
        if agent == "actions":
            return json.dumps(
                {"action_items": [{"task": "Write integration tests",
                                   "owner": "Bob", "deadline": "Friday"}]}
            )
        if agent == "decisions":
            return json.dumps(
                {"decisions": [{"decision": "Use Postgres",
                                "context": "Chosen for transactional safety"}]}
            )
        if agent == "follow_up":
            return json.dumps({"next_steps": ["Bob: open PR by Friday"]})
        return "{}"

    monkeypatch.setattr(pipeline, "_chat_raw", fake_chat_raw)
    # Reset compiled graph so monkeypatched fn is used
    pipeline._compiled = None

    out = await pipeline.summarize_transcript(sample_transcript)

    assert "cleaner" in calls
    assert out["summary"] == ["Coverage at 60%, target 80%."]
    assert out["action_items"][0]["task"] == "Write integration tests"
    assert out["decisions"][0]["decision"] == "Use Postgres"
    assert out["next_steps"]


@pytest.mark.asyncio
async def test_structured_repair_on_validation_failure(monkeypatch):
    """First response violates the schema (missing required field) → second call repairs it."""
    attempts = {"n": 0}

    async def fake_chat_raw(system, user, agent, json_mode=False):
        attempts["n"] += 1
        # First response: action item missing required `task` → ValidationError
        if attempts["n"] == 1:
            return json.dumps({"action_items": [{"owner": "Bob"}]})
        # Second response: valid
        return json.dumps(
            {"action_items": [{"task": "Ship the docs", "owner": "Bob"}]}
        )

    monkeypatch.setattr(pipeline, "_chat_raw", fake_chat_raw)

    from app.schemas import ActionItemsOutput

    out = await pipeline._chat_structured(
        "system", "user", "actions", ActionItemsOutput
    )
    assert attempts["n"] >= 2
    assert out.action_items[0].task == "Ship the docs"


@pytest.mark.asyncio
async def test_chat_retries_transient_errors(monkeypatch):
    """Tenacity should retry, then succeed."""
    attempts = {"n": 0}

    async def fake_chat_raw(system, user, agent, json_mode=False):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise RuntimeError("transient")
        return "ok"

    monkeypatch.setattr(pipeline, "_chat_raw", fake_chat_raw)
    text = await pipeline._chat("s", "u", agent="cleaner")
    assert text == "ok"
    assert attempts["n"] == 2


@pytest.mark.asyncio
async def test_empty_transcript_rejected():
    with pytest.raises(ValueError):
        await pipeline.summarize_transcript("")
