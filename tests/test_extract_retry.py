"""Transient-failure handling around the model call.

A rate limit is the *expected* failure on a free tier, not an exceptional one,
so it is worth pinning: the call retries, it respects the delay the API asks
for, and whatever finally surfaces is something a reviewer can act on rather
than a stack trace. None of this needs an API key.
"""

from __future__ import annotations

import pytest

from src import extract as ex
from src.ingest import IngestedDocument


class FakeStatusError(Exception):
    """Stands in for the SDK's private 429/5xx classes, which branch on status_code."""

    def __init__(self, status_code: int, message: str = ""):
        super().__init__(message or f"Error code: {status_code}")
        self.status_code = status_code


class FakeInteractions:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeClient:
    def __init__(self, outcomes):
        self.interactions = FakeInteractions(outcomes)


@pytest.fixture
def document():
    return IngestedDocument(filename="note.txt", kind="text", text="Temp 38.9 C", preview="")


@pytest.fixture
def no_sleep(monkeypatch):
    """Keep the suite fast, and record what the backoff would have waited."""
    waits: list[float] = []
    monkeypatch.setattr(ex.time, "sleep", lambda s: waits.append(s))
    return waits


def test_retries_then_succeeds(document, no_sleep):
    client = FakeClient([FakeStatusError(429), FakeStatusError(429), "ok"])
    assert ex._create(client, "m", document) == "ok"
    assert client.interactions.calls == 3
    assert len(no_sleep) == 2


def test_gives_up_after_max_attempts_with_an_actionable_message(document, no_sleep):
    client = FakeClient([FakeStatusError(429)] * ex.MAX_ATTEMPTS)
    with pytest.raises(ex.ExtractionError, match="Rate limited"):
        ex._create(client, "m", document)
    assert client.interactions.calls == ex.MAX_ATTEMPTS
    # One fewer sleep than attempts: no point waiting after the final failure.
    assert len(no_sleep) == ex.MAX_ATTEMPTS - 1


def test_server_errors_are_retried_too(document, no_sleep):
    client = FakeClient([FakeStatusError(503), "ok"])
    assert ex._create(client, "m", document) == "ok"
    assert client.interactions.calls == 2


@pytest.mark.parametrize("status", [400, 404, 422])
def test_client_errors_are_not_retried(document, no_sleep, status):
    client = FakeClient([FakeStatusError(status)])
    with pytest.raises(Exception) as caught:
        ex._create(client, "m", document)
    assert not isinstance(caught.value, ex.ExtractionError)
    assert client.interactions.calls == 1
    assert no_sleep == []


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors_explain_themselves_and_do_not_retry(document, no_sleep, status):
    client = FakeClient([FakeStatusError(status)])
    with pytest.raises(ex.ExtractionError, match="GEMINI_API_KEY"):
        ex._create(client, "m", document)
    assert client.interactions.calls == 1


def test_a_long_suggested_delay_fails_fast_instead_of_spending_quota(document, no_sleep):
    """Retrying past the cap cannot succeed, and each attempt costs a request."""
    client = FakeClient([FakeStatusError(429, "Please retry in 39.87s.")])
    with pytest.raises(ex.ExtractionError, match="wait about 40 seconds"):
        ex._create(client, "m", document)
    assert client.interactions.calls == 1
    assert no_sleep == []


def test_a_short_suggested_delay_is_still_waited_out(document, no_sleep):
    client = FakeClient([FakeStatusError(429, "Please retry in 2s."), "ok"])
    assert ex._create(client, "m", document) == "ok"
    assert client.interactions.calls == 2


def test_backoff_prefers_the_delay_the_api_asks_for():
    exc = FakeStatusError(429, "Please retry in 4.505092064s.")
    # The suggested delay, plus a small margin, rather than a blind doubling.
    assert 4.5 < ex._backoff_seconds(exc, attempt=0) < 6.0


def test_backoff_falls_back_to_exponential_and_stays_bounded():
    exc = FakeStatusError(429, "no hint here")
    assert ex._backoff_seconds(exc, attempt=0) == 1.0
    assert ex._backoff_seconds(exc, attempt=2) == 4.0
    assert ex._backoff_seconds(exc, attempt=99) == ex.MAX_BACKOFF_SECONDS


def test_a_suggested_delay_never_exceeds_the_cap():
    exc = FakeStatusError(429, "Please retry in 900s.")
    assert ex._backoff_seconds(exc, attempt=0) == ex.MAX_BACKOFF_SECONDS
