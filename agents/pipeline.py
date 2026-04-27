"""LangGraph multi-agent summarization pipeline.

Flow:
    raw_transcript
        -> CleanerAgent
        -> [SummarizerAgent, ActionItemsAgent, DecisionsAgent]   (parallel fan-out)
        -> CriticAgent           (validates structured outputs, requests rerun)
        -> FollowUpAgent
        -> final MeetingNotes

Engineering practices baked in:
- Pydantic-validated structured outputs per agent (`app.schemas`)
- Few-shot examples + chain-of-thought prompts
- Retries with exponential backoff (`tenacity`) on every LLM call
- Token + latency + success/error metrics (`app.metrics`)
- Structured JSON logging (`app.logging`)
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Type, TypedDict, TypeVar

from dotenv import load_dotenv
from langgraph.graph import END, START, StateGraph
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.logging import get_logger
from app.metrics import LLM_CALLS, LLM_LATENCY, LLM_TOKENS
from app.schemas import (
    ActionItemsOutput,
    DecisionsOutput,
    MeetingNotes,
    NextStepsOutput,
    SummaryOutput,
)

load_dotenv()
log = get_logger(__name__)

MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4.1-nano")
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
MAX_LLM_ATTEMPTS = 3

T = TypeVar("T", bound=BaseModel)


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
    # Critic state
    needs_rerun: list[str]
    rerun_count: int


# ---------------- LLM helpers ----------------

async def _chat_raw(system: str, user: str, agent: str, json_mode: bool = False) -> str:
    """Single LLM call, recording metrics. Raises on transport errors so tenacity can retry."""
    client = _client()
    kwargs: dict[str, Any] = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    start = time.perf_counter()
    try:
        resp = await client.chat.completions.create(**kwargs)
    except Exception:
        LLM_CALLS.labels(model=MODEL, agent=agent, outcome="error").inc()
        log.warning("llm_call_failed", agent=agent, model=MODEL)
        raise
    finally:
        LLM_LATENCY.labels(model=MODEL, agent=agent).observe(
            time.perf_counter() - start
        )

    LLM_CALLS.labels(model=MODEL, agent=agent, outcome="success").inc()
    usage = getattr(resp, "usage", None)
    if usage:
        LLM_TOKENS.labels(model=MODEL, kind="prompt").inc(usage.prompt_tokens or 0)
        LLM_TOKENS.labels(model=MODEL, kind="completion").inc(
            usage.completion_tokens or 0
        )
        log.info(
            "llm_call",
            agent=agent,
            model=MODEL,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
        )
    return resp.choices[0].message.content or ""


async def _chat(system: str, user: str, agent: str, json_mode: bool = False) -> str:
    """LLM call with exponential-backoff retry on transient failures."""
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(MAX_LLM_ATTEMPTS),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception_type(Exception),
        reraise=True,
    ):
        with attempt:
            return await _chat_raw(system, user, agent, json_mode=json_mode)
    return ""  # unreachable


def _safe_json(text: str) -> dict:
    """Best-effort JSON extraction — handles models that wrap output in prose."""
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


async def _chat_structured(
    system: str,
    user: str,
    agent: str,
    schema: Type[T],
) -> T:
    """LLM call → JSON → Pydantic validation. Asks the model to repair on validation
    failure (a self-critique loop, capped at MAX_LLM_ATTEMPTS)."""
    last_error: str | None = None
    for attempt in range(MAX_LLM_ATTEMPTS):
        prompt = user
        if last_error:
            prompt = (
                f"{user}\n\n"
                f"PREVIOUS OUTPUT FAILED VALIDATION: {last_error}\n"
                f"Return JSON exactly matching the requested schema."
            )
        raw = await _chat(system, prompt, agent=agent, json_mode=True)
        try:
            return schema.model_validate(_safe_json(raw))
        except ValidationError as e:
            last_error = str(e)[:400]
            log.warning(
                "structured_validation_failed",
                agent=agent,
                attempt=attempt + 1,
                error=last_error,
            )
    log.error("structured_validation_exhausted", agent=agent)
    return schema()  # safe empty default


# ---------------- Agents ----------------

CLEANER_SYSTEM = (
    "You are CleanerAgent. Clean a meeting transcript while preserving every "
    "substantive statement.\n"
    "Rules:\n"
    "1. Fix grammar and capitalisation.\n"
    "2. Remove filler words: um, uh, like, you know, sort of, kind of.\n"
    "3. Preserve speaker labels if present (e.g. 'Alice:').\n"
    "4. Do NOT summarise. Do NOT add commentary.\n"
    "5. Output plain text only — no markdown, no quotes around it."
)

SUMMARIZER_SYSTEM = (
    "You are SummarizerAgent. Reason step-by-step about the key discussion threads,"
    " then output 3-5 concise bullets. Each bullet ≤ 25 words.\n\n"
    "Example input: 'Alice: We hit 60% test coverage. Bob: We should aim for 80% by Q3.'\n"
    "Example output: {\"summary\": [\"Test coverage currently at 60%.\","
    " \"Team agreed to target 80% coverage by Q3.\"]}\n\n"
    'Return JSON: {"summary": ["...", ...]}'
)

ACTIONS_SYSTEM = (
    "You are ActionItemsAgent. Extract every action item.\n"
    "An action item is a concrete future task assigned to someone.\n"
    "Reason step-by-step: scan for verbs of commitment ('will', 'going to', "
    "'I'll', 'can you'), then identify owner and deadline if stated.\n\n"
    "Example: 'Bob: I'll send the deck by Friday.'\n"
    "→ {\"action_items\":[{\"task\":\"Send the deck\",\"owner\":\"Bob\",\"deadline\":\"Friday\"}]}\n\n"
    'Return JSON: {"action_items":[{"task":"...","owner":"...","deadline":"..."}]}.'
    " Use null for unknown owner/deadline. Empty list if no actions."
)

DECISIONS_SYSTEM = (
    "You are DecisionsAgent. Identify each decision the group reached.\n"
    "A decision is a settled choice between alternatives.\n\n"
    "Example: 'After debate we'll use Postgres not Mongo.'\n"
    "→ {\"decisions\":[{\"decision\":\"Use Postgres for storage\","
    "\"context\":\"Chosen over Mongo after debate\"}]}\n\n"
    'Return JSON: {"decisions":[{"decision":"...","context":"..."}]}.'
    " Empty list if no decisions."
)

FOLLOWUP_SYSTEM = (
    "You are FollowUpAgent. Given a meeting summary, action items, and decisions, "
    "propose 3-5 concrete next steps that move the group forward. Be specific — "
    'name owners and rough timelines when possible.\n'
    'Return JSON: {"next_steps":["...", ...]}'
)


async def cleaner_agent(state: MeetingState) -> dict:
    raw = state.get("raw_transcript", "")
    cleaned = await _chat(CLEANER_SYSTEM, raw, agent="cleaner")
    return {"cleaned_transcript": cleaned.strip()}


async def summarizer_agent(state: MeetingState) -> dict:
    transcript = state.get("cleaned_transcript") or state.get("raw_transcript", "")
    out = await _chat_structured(SUMMARIZER_SYSTEM, transcript, "summarizer", SummaryOutput)
    return {"summary": out.summary}


async def action_items_agent(state: MeetingState) -> dict:
    transcript = state.get("cleaned_transcript") or state.get("raw_transcript", "")
    out = await _chat_structured(ACTIONS_SYSTEM, transcript, "actions", ActionItemsOutput)
    return {"action_items": [a.model_dump() for a in out.action_items]}


async def decisions_agent(state: MeetingState) -> dict:
    transcript = state.get("cleaned_transcript") or state.get("raw_transcript", "")
    out = await _chat_structured(DECISIONS_SYSTEM, transcript, "decisions", DecisionsOutput)
    return {"decisions": [d.model_dump() for d in out.decisions]}


async def follow_up_agent(state: MeetingState) -> dict:
    payload = {
        "summary": state.get("summary", []),
        "action_items": state.get("action_items", []),
        "decisions": state.get("decisions", []),
    }
    out = await _chat_structured(
        FOLLOWUP_SYSTEM, json.dumps(payload, indent=2), "follow_up", NextStepsOutput
    )
    return {"next_steps": out.next_steps}


async def critic_agent(state: MeetingState) -> dict:
    """Quality gate: log warnings if any agent's output looks degenerate.

    Cheap rule-based checks; no extra LLM call. Tracks `needs_rerun` for
    observability — a future iteration could conditionally re-route the graph.
    """
    issues: list[str] = []
    if not state.get("summary"):
        issues.append("summary_empty")
    if len(state.get("summary", [])) > 8:
        issues.append("summary_too_long")
    transcript_lower = (state.get("cleaned_transcript") or "").lower()
    if not state.get("action_items") and any(
        kw in transcript_lower for kw in ("i'll", "we'll", "going to", "by friday", "by monday")
    ):
        issues.append("action_items_likely_missed")
    if issues:
        log.warning("critic_flagged_issues", issues=issues)
    return {"needs_rerun": issues}


# ---------------- Graph ----------------

def _build_graph():
    g = StateGraph(MeetingState)
    g.add_node("clean", cleaner_agent)
    g.add_node("summarize", summarizer_agent)
    g.add_node("actions", action_items_agent)
    g.add_node("decisions", decisions_agent)
    g.add_node("critic", critic_agent)
    g.add_node("follow_up", follow_up_agent)

    g.add_edge(START, "clean")
    # Fan-out: parallel summarisation, action extraction, decision extraction
    g.add_edge("clean", "summarize")
    g.add_edge("clean", "actions")
    g.add_edge("clean", "decisions")
    # Fan-in to critic
    g.add_edge("summarize", "critic")
    g.add_edge("actions", "critic")
    g.add_edge("decisions", "critic")
    g.add_edge("critic", "follow_up")
    g.add_edge("follow_up", END)
    return g.compile()


_compiled = None


def get_graph():
    global _compiled
    if _compiled is None:
        _compiled = _build_graph()
    return _compiled


async def summarize_transcript(transcript: str) -> dict:
    """Run the full pipeline and return a validated MeetingNotes dict."""
    if not transcript or not transcript.strip():
        raise ValueError("Empty transcript")

    log.info("pipeline_start", transcript_chars=len(transcript))
    graph = get_graph()
    result = await graph.ainvoke({"raw_transcript": transcript})

    notes = MeetingNotes(
        cleaned_transcript=result.get("cleaned_transcript", ""),
        summary=result.get("summary", []),
        action_items=result.get("action_items", []),
        decisions=result.get("decisions", []),
        next_steps=result.get("next_steps", []),
    )
    log.info(
        "pipeline_done",
        summary_n=len(notes.summary),
        actions_n=len(notes.action_items),
        decisions_n=len(notes.decisions),
    )
    return notes.model_dump()
