"""Evaluation harness — runs the LangGraph pipeline on labelled fixtures and
emits a markdown report with per-fixture pass/fail and aggregate metrics.

Usage:
    python -m eval.run

Metrics:
    - summary keyword recall (does the summary mention required topics?)
    - action-item recall (did we extract at least N items?)
    - decision recall
    - owner attribution accuracy
    - pipeline latency (wall-clock seconds)

Designed for CI: exits non-zero if aggregate score < SCORE_THRESHOLD.

For ASR (speech-to-text) eval, run `eval/run_asr.py` (separate, requires audio
fixtures + jiwer for WER).
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

from agents.pipeline import summarize_transcript
from app.logging import get_logger

log = get_logger(__name__)

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures.json"
SCORE_THRESHOLD = 0.6  # CI fails below this


def keyword_recall(text_haystack: str, keywords: list[str]) -> float:
    if not keywords:
        return 1.0
    hay = text_haystack.lower()
    hits = sum(1 for k in keywords if k.lower() in hay)
    return hits / len(keywords)


async def evaluate_one(fx: dict) -> dict:
    transcript = fx["transcript"]
    expected = fx["expected"]
    t0 = time.perf_counter()
    out = await summarize_transcript(transcript)
    latency_s = time.perf_counter() - t0

    summary_text = " ".join(out["summary"])
    summary_score = keyword_recall(summary_text, expected.get("summary_keywords", []))

    n_actions = len(out["action_items"])
    actions_ok = n_actions >= expected.get("expected_action_items_min", 0)

    n_decisions = len(out["decisions"])
    decisions_ok = n_decisions >= expected.get("expected_decisions_min", 0)

    expected_owners = {o.lower() for o in expected.get("expected_owners", [])}
    actual_owners = {
        (a.get("owner") or "").lower() for a in out["action_items"] if a.get("owner")
    }
    owner_recall = (
        len(expected_owners & actual_owners) / len(expected_owners)
        if expected_owners
        else 1.0
    )

    score = (
        0.35 * summary_score
        + 0.25 * (1.0 if actions_ok else 0.0)
        + 0.15 * (1.0 if decisions_ok else 0.0)
        + 0.25 * owner_recall
    )

    return {
        "id": fx["id"],
        "score": round(score, 3),
        "summary_recall": round(summary_score, 3),
        "actions_ok": actions_ok,
        "decisions_ok": decisions_ok,
        "owner_recall": round(owner_recall, 3),
        "latency_s": round(latency_s, 2),
        "n_actions": n_actions,
        "n_decisions": n_decisions,
    }


async def main() -> int:
    fixtures = json.loads(FIXTURES.read_text(encoding="utf-8"))["transcripts"]
    results = []
    for fx in fixtures:
        log.info("eval_running", id=fx["id"])
        try:
            results.append(await evaluate_one(fx))
        except Exception as e:
            log.error("eval_failed", id=fx["id"], error=str(e))
            results.append({"id": fx["id"], "score": 0.0, "error": str(e)})

    avg = sum(r["score"] for r in results) / len(results)
    avg_latency = sum(r.get("latency_s", 0) for r in results) / len(results)

    print("\n# Evaluation Report\n")
    print(f"- Fixtures: **{len(results)}**")
    print(f"- Aggregate score: **{avg:.3f}** (threshold {SCORE_THRESHOLD})")
    print(f"- Mean pipeline latency: **{avg_latency:.2f}s**\n")
    print("| id | score | summary | actions | decisions | owners | latency |")
    print("|----|------:|--------:|:-------:|:---------:|------:|--------:|")
    for r in results:
        print(
            f"| {r['id']} | {r['score']} | {r.get('summary_recall', '-')} "
            f"| {'PASS' if r.get('actions_ok') else 'FAIL'} "
            f"| {'PASS' if r.get('decisions_ok') else 'FAIL'} "
            f"| {r.get('owner_recall', '-')} | {r.get('latency_s', '-')}s |"
        )

    return 0 if avg >= SCORE_THRESHOLD else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
