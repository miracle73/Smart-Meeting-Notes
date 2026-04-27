# Peer-Review Checklist

A concrete rubric I use when reviewing other capstones, mirroring the scoring categories. Each item asks for a specific, defensible observation — not "good job."

## 1. Technical depth (20%)
- Is the problem real and well-scoped? Cite the user pain in one sentence.
- Are prompts deliberate? Look for: few-shot examples, chain-of-thought reasoning, structured-output schemas. Quote a prompt and explain what makes it good or weak.
- Multi-step orchestration: are there parallel branches? Critic / validation steps? Or is it a single LLM call wearing a costume?
- Trade-offs called out in a doc: Whisper vs. managed ASR, sequential vs. parallel agents, in-memory vs. persistent state, etc. If unstated, flag it.

## 2. Engineering practices (20%)
- **Code hygiene**: any obvious AI-slop (unused imports, dead code, `# Note: this function does X` over self-explanatory code, inconsistent naming)?
- **Logging**: `print()` in production paths = -1. Is logging structured (JSON) with `request_id`, `duration_ms`, `stage`?
- **Tests**: are there unit tests with mocked LLM/ASR? Integration tests for the HTTP/WS surface? Run `pytest -q` — does it pass cold without secrets?
- **Error handling**: do user-facing errors translate exceptions into actionable messages, or does the user see `[Errno 11001] getaddrinfo failed`?
- **Retries / backoff**: any tenacity / manual loop around external API calls? What's the max-attempts policy?

## 3. Production readiness (15%)
- Can I `docker compose up` and have a working service in under 2 minutes?
- Is there a `/health` and a `/metrics` endpoint?
- Is there an evaluation harness I can run that emits a number? Does it have a CI threshold?
- Is the deploy real (Render/Fly/HF Space URL works, not a screenshot)?

## 4. Presentation (15%)
- Does the live demo match the README? Time it.
- Is there an architecture diagram (Mermaid is fine) and does it match the code?
- Are trade-offs explained verbally, not just listed?

## 5. Specific feedback to deliver

For each project I review I produce:
- **3 things they did well** — specific, technical (e.g. "Critic node short-circuits on validation failure rather than a second LLM call — saves ~$0.002/run").
- **3 concrete improvements** — each with a file/line pointer (e.g. "`agents/pipeline.py:55`: tenacity wraps the wrong layer; if the JSON parse fails it won't retry. Move it inside `_chat_structured`").
- **1 architectural question** — surfaces a trade-off they may not have considered (e.g. "Why a Pydantic critic instead of an LLM-based one? Have you measured the false-negative rate on missed action items?").

This shape is intentional: avoids generic praise, forces me to actually read the code, and makes the feedback useful enough that the author wants to action it.
