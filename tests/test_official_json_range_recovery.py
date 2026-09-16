from __future__ import annotations

from http.client import IncompleteRead
import json
import re

import pytest

# Initialize the data-platform package through its public loader path; the
# legacy module graph imports taiwan_official back from security_loader.
from stock_ai.data_platform import security_loader as _security_loader  # noqa: F401
import stock_ai.taiwan_official as official


class Response:
    def __init__(self, *, body: bytes, status: int, headers: dict[str, str], incomplete_at: int | None = None):
        self.body = body
        self.status = status
        self.headers = headers
        self.incomplete_at = incomplete_at

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        if self.incomplete_at is not None:
            raise IncompleteRead(self.body[: self.incomplete_at], len(self.body) - self.incomplete_at)
        return self.body


def _payload() -> bytes:
    return json.dumps([{"code": str(index), "name": "離線" * 20} for index in range(2400)],
                      ensure_ascii=False).encode()


def test_incomplete_official_json_is_resumed_with_version_checked_ranges(monkeypatch):
    payload = _payload()
    calls: list[dict[str, str]] = []
    etag = '"fixture-v1"'
    modified = "Sun, 13 Sep 2026 14:03:59 GMT"

    def open_fixture(request, timeout):
        assert timeout == 20
        headers = dict(request.header_items())
        calls.append(headers)
        byte_range = headers.get("Range")
        if byte_range is None:
            return Response(body=payload, status=200, incomplete_at=15997, headers={
                "Content-Length": str(len(payload)), "Accept-Ranges": "bytes",
                "ETag": etag, "Last-Modified": modified,
            })
        match = re.fullmatch(r"bytes=(\d+)-(\d+)", byte_range)
        assert match and headers["If-range"] == etag
        start, end = map(int, match.groups())
        return Response(body=payload[start : end + 1], status=206, headers={
            "Content-Range": f"bytes {start}-{end}/{len(payload)}",
            "ETag": etag, "Last-Modified": modified,
        })

    monkeypatch.setattr(official, "urlopen", open_fixture)
    assert official._read_official_json_bytes("https://official.invalid/large.json", timeout=20) == payload
    ranges = [headers["Range"] for headers in calls[1:]]
    assert ranges[0].startswith("bytes=15997-")
    assert len(ranges) >= 2


def test_range_resume_rejects_a_changed_official_version(monkeypatch):
    payload = _payload()

    def open_fixture(request, timeout):
        assert timeout == 20
        headers = dict(request.header_items())
        byte_range = headers.get("Range")
        if byte_range is None:
            return Response(body=payload, status=200, incomplete_at=15997, headers={
                "Content-Length": str(len(payload)), "Accept-Ranges": "bytes", "ETag": '"v1"',
            })
        start, end = map(int, re.fullmatch(r"bytes=(\d+)-(\d+)", byte_range).groups())
        return Response(body=payload[start : end + 1], status=206, headers={
            "Content-Range": f"bytes {start}-{end}/{len(payload)}", "ETag": '"v2"',
        })

    monkeypatch.setattr(official, "urlopen", open_fixture)
    with pytest.raises(ValueError, match="official_json_range_response_mismatch"):
        official._read_official_json_bytes("https://official.invalid/changed.json", timeout=20)


def test_incomplete_response_without_range_proof_remains_a_failure(monkeypatch):
    payload = _payload()
    monkeypatch.setattr(official, "urlopen", lambda *_args, **_kwargs: Response(
        body=payload, status=200, incomplete_at=15997, headers={"Content-Length": str(len(payload))},
    ))
    with pytest.raises(IncompleteRead):
        official._read_official_json_bytes("https://official.invalid/no-range.json", timeout=20)


def test_tpex_index_series_honors_the_shared_dataset_request_interval(monkeypatch):
    sleeps: list[float] = []
    calls: list[str] = []
    official.clear_official_caches()
    monkeypatch.setattr(official.time, "sleep", sleeps.append)
    monkeypatch.setattr(official, "_get_json", lambda url: calls.append(url) or [{"Date": "2026-09-14"}])
    rows = official.tpex_indices()
    assert len(rows) == len(calls) == len(official.TPEX_INDEX_SERIES)
    assert sleeps == [1.0] * (len(official.TPEX_INDEX_SERIES) - 1)
