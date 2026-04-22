"""LangGraph multi-agent summarization pipeline.

Flow:
    raw_transcript
        -> TranscriptCleanerAgent
        -> [SummarizerAgent, ActionItemExtractorAgent, DecisionExtractorAgent]   (parallel)
        -> FollowUpAgent
        -> final structured output
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, TypedDict

from dotenv import load_dotenv
from langgraph.graph import END, START, StateGraph
from openai import AsyncOpenAI

load_dotenv()

MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4.1-nano")
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


def _client() -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=os.getenv("OPENAI_API_KEY"),
        base_url=os.getenv("OPENAI_BASE_URL", DEFAULT_BASE_URL),
    )


class MeetingState(TypedDict, total=False):
    raw_transcript: str
    cleaned_transcript: str
    summary: list
    action_items: list
    decisions: list
    next_steps: list


async def _chat(system: str, user: str, json_mode: bool = False) -> str:
    client = _client()
    kwargs: dict[str, Any] = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = await client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content or ""


def _safe_json(text: str) -> dict:
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
        return {}


# ---------------- Agents ----------------

async def cleaner_agent(state: MeetingState) -> dict:
    raw = state.get("raw_transcript", "")
    system = (
        "You are TranscriptCleanerAgent. Clean the meeting transcript: "
        "fix grammar, remove filler words (um, uh, like, you know, sort of), "
        "preserve speaker labels if present (e.g. 'Alice:'), keep all substantive content. "
        "Return ONLY the cleaned transcript as plain text — no commentary."
    )
    cleaned = await _chat(system, raw)
    return {"cleaned_transcript": cleaned.strip()}


async def summarizer_agent(state: MeetingState) -> dict:
    transcript = state.get("cleaned_transcript") or state.get("raw_transcript", "")
    system = (
        "You are SummarizerAgent. Produce a concise summary of the meeting "
        "as 3-5 bullet points covering key discussion points. "
        'Return JSON: {"summary": ["point 1", "point 2", ...]}'
    )
    data = _safe_json(await _chat(system, transcript, json_mode=True))
    return {"summary": data.get("summary", [])}


async def action_items_agent(state: MeetingState) -> dict:
    transcript = state.get("cleaned_transcript") or state.get("raw_transcript", "")
    system = (
        "You are ActionItemExtractorAgent. Extract every action item from the meeting. "
        "For each item include: task (description), owner (responsible person or null), "
        "deadline (if mentioned, else null). "
        'Return JSON: {"action_items": [{"task": "...", "owner": "...", "deadline": "..."}, ...]}. '
        "Return an empty list if none."
    )
    data = _safe_json(await _chat(system, transcript, json_mode=True))
    return {"action_items": data.get("action_items", [])}


async def decisions_agent(state: MeetingState) -> dict:
    transcript = state.get("cleaned_transcript") or state.get("raw_transcript", "")
    system = (
        "You are DecisionExtractorAgent. Identify each decision made during the meeting. "
        'Return JSON: {"decisions": [{"decision": "...", "context": "..."}, ...]}. '
        "Return an empty list if no decisions were made."
    )
    data = _safe_json(await _chat(system, transcript, json_mode=True))
    return {"decisions": data.get("decisions", [])}


async def follow_up_agent(state: MeetingState) -> dict:
    payload = {
        "summary": state.get("summary", []),
        "action_items": state.get("action_items", []),
        "decisions": state.get("decisions", []),
    }
    system = (
        "You are FollowUpAgent. Given the meeting summary, action items, and decisions, "
        "suggest 3-5 concrete next steps and follow-up recommendations that tie it all together. "
        'Return JSON: {"next_steps": ["step 1", "step 2", ...]}'
    )
    data = _safe_json(await _chat(system, json.dumps(payload, indent=2), json_mode=True))
    return {"next_steps": data.get("next_steps", [])}


# ---------------- Graph ----------------

def _build_graph():
    g = StateGraph(MeetingState)
    g.add_node("clean", cleaner_agent)
    g.add_node("summarize", summarizer_agent)
    g.add_node("actions", action_items_agent)
    g.add_node("decisions", decisions_agent)
    g.add_node("follow_up", follow_up_agent)

    g.add_edge(START, "clean")
    # Fan-out: run summarizer, action items, and decisions in parallel
    g.add_edge("clean", "summarize")
    g.add_edge("clean", "actions")
    g.add_edge("clean", "decisions")
    # Fan-in: follow-up waits for all three
    g.add_edge("summarize", "follow_up")
    g.add_edge("actions", "follow_up")
    g.add_edge("decisions", "follow_up")
    g.add_edge("follow_up", END)
    return g.compile()


_compiled = None


def get_graph():
    global _compiled
    if _compiled is None:
        _compiled = _build_graph()
    return _compiled


async def summarize_transcript(transcript: str) -> dict:
    """Run the full pipeline on a raw transcript and return structured output."""
    if not transcript or not transcript.strip():
        raise ValueError("Empty transcript")

    graph = get_graph()
    result = await graph.ainvoke({"raw_transcript": transcript})
    return {
        "cleaned_transcript": result.get("cleaned_transcript", ""),
        "summary": result.get("summary", []),
        "action_items": result.get("action_items", []),
        "decisions": result.get("decisions", []),
        "next_steps": result.get("next_steps", []),
    }
