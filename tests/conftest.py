"""Shared pytest fixtures.

We never hit AssemblyAI or OpenRouter from the test suite — both are mocked.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make project root importable
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Make sure agents read predictable env at import time
os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("OPENAI_BASE_URL", "https://example.invalid/v1")
os.environ.setdefault("ASSEMBLYAI_API_KEY", "test-key")
os.environ.setdefault("LOG_LEVEL", "WARNING")  # quiet test output


@pytest.fixture
def sample_transcript() -> str:
    return (
        "Alice: We hit 60% test coverage this sprint. We should aim for 80% by Q3.\n"
        "Bob: Agreed. I'll write the integration tests by Friday.\n"
        "Carol: Let's also decide on Postgres versus Mongo for storage.\n"
        "Alice: We discussed it earlier — going with Postgres for transactional safety."
    )
