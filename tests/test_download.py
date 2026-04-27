"""Tests for the URL download error-handling layer.

We never hit the real network — `httpx.stream` is monkeypatched to return
synthetic responses so we can exercise every error branch.
"""
from __future__ import annotations

import contextlib
from types import SimpleNamespace

import httpx
import pytest

import main


class _FakeResp:
    def __init__(self, status_code: int, content_type: str = "audio/mpeg",
                 body: bytes = b"\x00" * 10):
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self._body = body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None,
                response=SimpleNamespace(status_code=self.status_code),
            )

    def iter_bytes(self):
        yield self._body


def _patch_stream(monkeypatch, resp_or_exc):
    @contextlib.contextmanager
    def fake_stream(*a, **kw):
        if isinstance(resp_or_exc, Exception):
            raise resp_or_exc
        yield resp_or_exc

    monkeypatch.setattr(main.httpx, "stream", fake_stream)


def test_download_404_friendly_message(monkeypatch, tmp_path):
    _patch_stream(monkeypatch, _FakeResp(404))
    with pytest.raises(RuntimeError, match="404"):
        main.download_from_url("https://example.com/file.mp3", str(tmp_path))


def test_download_503_friendly_message(monkeypatch, tmp_path):
    _patch_stream(monkeypatch, _FakeResp(503))
    with pytest.raises(RuntimeError, match="temporarily unavailable"):
        main.download_from_url("https://example.com/file.mp3", str(tmp_path))


def test_download_html_response_rejected(monkeypatch, tmp_path):
    _patch_stream(monkeypatch, _FakeResp(200, content_type="text/html"))
    with pytest.raises(RuntimeError, match="web page, not a media file"):
        # .mp3 extension forces the direct-httpx branch (vs yt-dlp)
        main.download_from_url("https://example.com/page.mp3", str(tmp_path))


def test_download_dns_failure(monkeypatch, tmp_path):
    _patch_stream(monkeypatch, httpx.ConnectError("getaddrinfo failed"))
    with pytest.raises(RuntimeError, match="Could not reach"):
        main.download_from_url("https://nope.invalid/x.mp3", str(tmp_path))


def test_download_success_writes_file(monkeypatch, tmp_path):
    _patch_stream(monkeypatch, _FakeResp(200, body=b"hello"))
    out = main.download_from_url("https://example.com/song.mp3", str(tmp_path))
    assert out.endswith(".mp3")
    with open(out, "rb") as f:
        assert f.read() == b"hello"
