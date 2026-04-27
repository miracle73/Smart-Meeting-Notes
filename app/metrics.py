"""Prometheus metrics: latency, success/failure rates, token usage.

Exposed at GET /metrics. Scrape from Prometheus / Grafana Cloud / Datadog.
"""
from __future__ import annotations

import time
from contextlib import contextmanager

from prometheus_client import Counter, Histogram

# ---- HTTP / pipeline ----
PIPELINE_LATENCY = Histogram(
    "smn_pipeline_latency_seconds",
    "End-to-end latency per pipeline stage",
    labelnames=("stage",),
    buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600),
)

PIPELINE_RESULT = Counter(
    "smn_pipeline_result_total",
    "Pipeline outcomes per stage",
    labelnames=("stage", "outcome"),  # outcome = success|error
)

# ---- LLM ----
LLM_TOKENS = Counter(
    "smn_llm_tokens_total",
    "LLM token usage",
    labelnames=("model", "kind"),  # kind = prompt|completion
)

LLM_CALLS = Counter(
    "smn_llm_calls_total",
    "LLM call outcomes",
    labelnames=("model", "agent", "outcome"),
)

LLM_LATENCY = Histogram(
    "smn_llm_latency_seconds",
    "LLM call latency",
    labelnames=("model", "agent"),
    buckets=(0.25, 0.5, 1, 2, 5, 10, 20, 30, 60),
)

# ---- WebSocket / streaming ----
WS_SESSIONS = Counter(
    "smn_ws_sessions_total",
    "Live transcription WS sessions",
    labelnames=("outcome",),  # opened|closed|errored
)

WS_TURNS = Counter(
    "smn_ws_turns_total",
    "Number of finalised turns from AAI",
)


@contextmanager
def stage_timer(stage: str):
    """Context manager that records latency + success/error counter."""
    start = time.perf_counter()
    try:
        yield
        outcome = "success"
    except Exception:
        outcome = "error"
        raise
    finally:
        PIPELINE_LATENCY.labels(stage=stage).observe(time.perf_counter() - start)
        PIPELINE_RESULT.labels(stage=stage, outcome=outcome).inc()
