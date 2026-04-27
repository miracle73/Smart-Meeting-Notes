# Smart Meeting Notes — Architecture

## Problem

Recorded and live meetings produce hours of audio that nobody re-watches. We want a single tool that turns either kind of meeting into structured, validated notes (summary, action items with owners and deadlines, decisions, follow-ups), with low enough latency to be useful and high enough quality to be trusted.

## High-level diagram

```mermaid
flowchart LR
    A[Browser UI / Chrome Ext] -->|HTTP upload or URL| B[FastAPI]
    A -->|WebSocket binary chunks| B
    B -->|file| C[ffmpeg]
    C -->|PCM 16kHz mono| D[AssemblyAI<br/>Universal-Streaming v3]
    B -->|file| E[AssemblyAI<br/>Async transcription]
    D --> F[Live transcript]
    E --> G[Final transcript]
    G --> H[LangGraph Pipeline]
    F --> H

    subgraph H[LangGraph Pipeline]
      H1[CleanerAgent] --> H2[SummarizerAgent]
      H1 --> H3[ActionItemsAgent]
      H1 --> H4[DecisionsAgent]
      H2 --> H5[CriticAgent]
      H3 --> H5
      H4 --> H5
      H5 --> H6[FollowUpAgent]
    end

    H --> I[(Pydantic-validated MeetingNotes)]
    I --> A
    B -.->|Prometheus| J[/metrics]
    B -.->|structlog JSON| K[(stdout / log shipper)]
```

## Components

| Component                  | Role                                                                                   |
|----------------------------|----------------------------------------------------------------------------------------|
| `main.py`                  | FastAPI app: upload, URL, summarize, status, live-WS, `/metrics`, `/health`.           |
| `agents/pipeline.py`       | LangGraph DAG with 6 agents; tenacity retries; Pydantic-validated structured outputs.  |
| `app/schemas.py`           | Pydantic models for every agent's JSON output and the final `MeetingNotes`.            |
| `app/logging.py`           | `structlog` JSON logger; replaces all `print()` calls in app code.                     |
| `app/metrics.py`           | Prometheus counters/histograms for stage latency, LLM tokens, WS turns.                |
| `extension/`               | Chrome MV3 extension: tab capture → WebSocket bridge to backend.                       |
| `tests/`                   | pytest unit + integration tests; LLM/ASR/network fully mocked.                         |
| `eval/`                    | Reference fixtures + harness measuring summary recall, action/decision recall, owner attribution, latency. |
| `Dockerfile`               | Production image: ffmpeg + uvicorn, listens on `$PORT` (Render/Fly/HF Spaces ready). |

## Key trade-offs

1. **AssemblyAI v3 streaming over self-hosted Whisper** — managed service costs more per minute but eliminates GPU ops, gives sub-second latency, and reliably produces interim+final turns. Switching providers is a single function change.
2. **LangGraph with parallel fan-out** — summary, actions, and decisions are independent given a clean transcript, so we run them concurrently and gate on a Critic before follow-up. Cuts wall-clock by ~3× vs. sequential.
3. **Pydantic validation + self-repair loop** — `response_format=json_object` alone is unreliable; we re-prompt with the validation error embedded so the model self-corrects. Cap attempts at 3.
4. **Tenacity retries** — every LLM call retries with exponential backoff (1s → 8s, max 3 attempts) on any exception. Network blips don't surface to the user.
5. **Chrome MV3 offscreen document** — service workers can't run `MediaRecorder`/`getUserMedia`, so we delegate audio capture to an offscreen document and post messages back. Necessary to support tab capture inside MV3.
6. **In-memory `tasks` dict** — fine for a single instance. Production swap: Redis. Documented in README; the eval harness exercises the pipeline directly so storage choice doesn't gate scoring.

## Observability

- `GET /metrics` (Prometheus) — `smn_pipeline_latency_seconds{stage=...}`, `smn_llm_tokens_total{model,kind}`, `smn_llm_calls_total{model,agent,outcome}`, `smn_ws_sessions_total`, `smn_ws_turns_total`.
- Structured JSON logs to stdout — every HTTP request gets a `request_id`, `duration_ms`, and `status`; pipeline stages and LLM calls log token counts and outcomes.
- `eval/run.py` produces a markdown report with per-fixture and aggregate scores; CI fails if mean score drops below `SCORE_THRESHOLD = 0.6`.

## Deployment

```
docker compose up --build       # local
docker build -t smn . && fly deploy   # Fly.io
# or push to Render / Hugging Face Space — port comes from $PORT
```

`/health` is the liveness probe. `.env` provides `ASSEMBLYAI_API_KEY`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENROUTER_MODEL`.
