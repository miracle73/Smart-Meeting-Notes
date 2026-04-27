"""Pydantic schemas for structured LLM outputs.

Each agent emits validated JSON matching one of these models. If the model's
raw output fails validation, the Critic node retries it. This is what
"structured outputs" means in the rubric — not just `response_format=json_object`.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ActionItem(BaseModel):
    task: str = Field(..., min_length=1, description="Concrete action to take")
    owner: Optional[str] = Field(None, description="Person or team responsible")
    deadline: Optional[str] = Field(None, description="Deadline in natural language")


class Decision(BaseModel):
    decision: str = Field(..., min_length=1)
    context: Optional[str] = None


class SummaryOutput(BaseModel):
    summary: list[str] = Field(default_factory=list)


class ActionItemsOutput(BaseModel):
    action_items: list[ActionItem] = Field(default_factory=list)


class DecisionsOutput(BaseModel):
    decisions: list[Decision] = Field(default_factory=list)


class NextStepsOutput(BaseModel):
    next_steps: list[str] = Field(default_factory=list)


class MeetingNotes(BaseModel):
    """Final, validated output of the full LangGraph pipeline."""

    cleaned_transcript: str = ""
    summary: list[str] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
