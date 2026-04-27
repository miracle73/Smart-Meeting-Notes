"""Unit tests for the structured-output Pydantic schemas."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas import (
    ActionItem,
    ActionItemsOutput,
    Decision,
    MeetingNotes,
)


def test_action_item_requires_task():
    with pytest.raises(ValidationError):
        ActionItem(owner="Bob")  # missing task


def test_action_item_optional_fields_default_none():
    a = ActionItem(task="Send the deck")
    assert a.owner is None
    assert a.deadline is None


def test_decision_minimal():
    d = Decision(decision="Use Postgres")
    assert d.context is None


def test_action_items_output_round_trip():
    out = ActionItemsOutput(
        action_items=[{"task": "Ship docs", "owner": "Alice", "deadline": "Friday"}]
    )
    assert out.action_items[0].task == "Ship docs"


def test_meeting_notes_defaults_empty():
    n = MeetingNotes()
    assert n.summary == []
    assert n.action_items == []
    assert n.decisions == []
    assert n.next_steps == []
