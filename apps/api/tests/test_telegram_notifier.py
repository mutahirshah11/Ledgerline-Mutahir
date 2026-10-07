"""TelegramNotifier over httpx.MockTransport (T021; F-10.R5, C21, F-11.AC4, research R3/R8)."""

import json
import logging
import traceback
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.core.errors import UpstreamFailed
from app.domain.enums import Fault
from app.services.telegram import TelegramNotifier

TOKEN = "".join(["987654", ":", "TEST", "x" * 30])  # built at runtime, never a literal token
CHAT = "-100123"


def _settings(**over: object) -> Settings:
    base: dict[str, object] = {"TELEGRAM_BOT_TOKEN": SecretStr(TOKEN)}
    return Settings(_env_file=None, **(base | over))  # type: ignore[arg-type]


def _ok(message_id: int = 42) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": {"message_id": message_id}})


class _Handler:
    """Answers requests from a script (responses or exceptions); records requests."""

    def __init__(self, *answers: httpx.Response | Exception) -> None:
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


def _notifier(handler: _Handler, waits: list[float], **settings: object) -> TelegramNotifier:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return TelegramNotifier(_settings(**settings), client=client, sleep=waits.append)


def test_send_message_plain_text_no_parse_mode() -> None:
    handler = _Handler(_ok(42))
    waits: list[float] = []
    text = "LEDGERLINE FAILED · Sep 2026 · run 7f3c…\n<b>not html</b>"
    assert _notifier(handler, waits).send_message(CHAT, text) == 42
    (req,) = handler.requests
    assert req.method == "POST"
    assert str(req.url) == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert json.loads(req.content) == {"chat_id": CHAT, "text": text}
    assert req.extensions["timeout"]["read"] == 10
    assert waits == []


def test_retries_then_upstream_failed() -> None:
    handler = _Handler(httpx.Response(502), httpx.ReadTimeout("slow"), httpx.Response(503))
    waits: list[float] = []
    with pytest.raises(UpstreamFailed) as info:
        _notifier(handler, waits).send_message(CHAT, "hi")
    assert len(handler.requests) == 3
    assert waits == [1, 4]  # C21: 3 attempts, waits 1 s and 4 s
    assert info.value.service == "telegram"
    assert info.value.detail == "server error after 3 attempts"


def test_retry_then_success() -> None:
    handler = _Handler(httpx.ConnectError("refused"), _ok(7))
    waits: list[float] = []
    assert _notifier(handler, waits).send_message(CHAT, "hi") == 7
    assert waits == [1]


def test_client_error_not_retried() -> None:
    handler = _Handler(httpx.Response(400, json={"ok": False, "description": "chat not found"}))
    waits: list[float] = []
    with pytest.raises(UpstreamFailed) as info:
        _notifier(handler, waits).send_message(CHAT, "hi")
    assert len(handler.requests) == 1 and waits == []
    assert info.value.detail == "rejected: HTTP 400"


def test_telegram_down_fault_raises_after_retries() -> None:
    handler = _Handler(_ok())
    waits: list[float] = []
    client = httpx.Client(transport=httpx.MockTransport(handler))
    notifier = TelegramNotifier(
        _settings(), client=client, sleep=waits.append, fault=Fault.TELEGRAM_DOWN
    )
    with pytest.raises(UpstreamFailed) as info:
        notifier.send_message(CHAT, "hi")
    assert handler.requests == [] and waits == [1, 4]
    assert info.value.detail == "down after 3 attempts"


def test_missing_token_raises_without_calling() -> None:
    handler = _Handler(_ok())
    client = httpx.Client(transport=httpx.MockTransport(handler))
    notifier = TelegramNotifier(Settings(_env_file=None), client=client, sleep=lambda _: None)
    with pytest.raises(UpstreamFailed):
        notifier.send_message(CHAT, "hi")
    assert handler.requests == []


def test_bot_token_never_in_raised_error_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    def leaky(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    for handler in (
        _Handler(_ok()),
        leaky,
        _Handler(httpx.Response(500)),
        _Handler(httpx.Response(403)),
    ):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        notifier = TelegramNotifier(_settings(), client=client, sleep=lambda _: None)
        try:
            notifier.send_message(CHAT, "hi")
        except UpstreamFailed as exc:
            chain: list[BaseException | None] = [exc, exc.__cause__, exc.__context__]
            assert all(TOKEN not in f"{e!s} {e!r}" for e in chain if e is not None)
            assert exc.__cause__ is None
            assert exc.__context__ is None or exc.__suppress_context__
            assert TOKEN not in "".join(traceback.format_exception(exc))
    assert caplog.records, "httpx logs the request line; it must be redacted, not silenced"
    assert TOKEN not in caplog.text
    assert all(TOKEN not in r.getMessage() for r in caplog.records)


def test_rate_limited_429_is_retried() -> None:
    handler = _Handler(httpx.Response(429), _ok(9))
    waits: list[float] = []
    assert _notifier(handler, waits).send_message(CHAT, "hi") == 9
    assert len(handler.requests) == 2 and waits == [1]


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(200, content=b"<html>not json</html>"),
        httpx.Response(200, json={"ok": False, "result": {"message_id": 1}}),
    ],
    ids=["non-json", "ok-false"],
)
def test_bad_200_answer_rejected_without_retry(answer: httpx.Response) -> None:
    handler = _Handler(answer)
    waits: list[float] = []
    with pytest.raises(UpstreamFailed) as info:
        _notifier(handler, waits).send_message(CHAT, "hi")
    assert len(handler.requests) == 1 and waits == []
    assert info.value.detail.startswith("rejected")


def test_close_and_context_manager_close_the_client() -> None:
    client = httpx.Client(transport=httpx.MockTransport(_Handler(_ok())))
    with TelegramNotifier(_settings(), client=client, sleep=lambda _: None) as notifier:
        assert notifier.send_message(CHAT, "hi") == 42
    assert client.is_closed
    other = httpx.Client(transport=httpx.MockTransport(_Handler(_ok())))
    TelegramNotifier(_settings(), client=other, sleep=lambda _: None).close()
    assert other.is_closed


def test_log_filter_installed_once_by_constructor(monkeypatch: pytest.MonkeyPatch) -> None:
    httpx_log = logging.getLogger("httpx")
    monkeypatch.setattr(httpx_log, "filters", [])
    client = httpx.Client(transport=httpx.MockTransport(_Handler(_ok())))
    TelegramNotifier(_settings(), client=client, sleep=lambda _: None)
    TelegramNotifier(_settings(), client=client, sleep=lambda _: None)
    assert len(httpx_log.filters) == 1


def test_upstream_failed_reports_attempts() -> None:
    for answer, attempts in ((httpx.Response(503), 3), (httpx.Response(400), 1)):
        with pytest.raises(UpstreamFailed) as info:
            _notifier(_Handler(answer), []).send_message(CHAT, "hi")
        assert info.value.attempts == attempts


def test_on_retry_hears_each_retry() -> None:
    heard: list[dict[str, Any]] = []
    handler = _Handler(httpx.Response(502), httpx.Response(429), _ok(5))
    client = httpx.Client(transport=httpx.MockTransport(handler))
    notifier = TelegramNotifier(
        _settings(), client=client, sleep=lambda _: None, on_retry=heard.append
    )
    assert notifier.send_message(CHAT, "hi") == 5
    assert heard == [
        {"service": "telegram", "attempt": 2, "cause": "server error"},
        {"service": "telegram", "attempt": 3, "cause": "rate limited"},
    ]


def test_no_backoff_retries_without_waiting() -> None:
    handler = _Handler(httpx.Response(500))
    waits: list[float] = []
    with pytest.raises(UpstreamFailed):
        _notifier(handler, waits, TELEGRAM_BACKOFF_S=()).send_message(CHAT, "hi")
    assert len(handler.requests) == 3
    assert all(w == 0 for w in waits)


# --- sendDocument (005: F-10 sends report.html) --------------------------------------------

REPORT = b"<html><body>report 4417</body></html>"


def test_send_document_multipart() -> None:
    handler = _Handler(_ok(77))
    waits: list[float] = []
    sent = _notifier(handler, waits).send_document(CHAT, "report.html", REPORT, caption="Sep")
    assert sent == 77
    (req,) = handler.requests
    assert req.method == "POST"
    assert str(req.url) == f"https://api.telegram.org/bot{TOKEN}/sendDocument"
    assert req.headers["content-type"].startswith("multipart/form-data")
    body = req.content
    assert b'name="chat_id"' in body and CHAT.encode() in body
    assert b'name="caption"' in body and b"Sep" in body
    assert b'name="document"; filename="report.html"' in body and REPORT in body
    assert req.extensions["timeout"]["read"] == 10
    assert waits == []


def test_send_document_without_caption_sends_no_caption_field() -> None:
    handler = _Handler(_ok(5))
    assert _notifier(handler, []).send_document(CHAT, "report.html", REPORT) == 5
    assert b'name="caption"' not in handler.requests[0].content


def test_send_document_retries_then_upstream_failed() -> None:
    handler = _Handler(httpx.Response(502), httpx.ConnectError("refused"), httpx.Response(429))
    waits: list[float] = []
    with pytest.raises(UpstreamFailed) as info:
        _notifier(handler, waits).send_document(CHAT, "report.html", REPORT)
    assert len(handler.requests) == 3 and waits == [1, 4]
    assert info.value.detail == "rate limited after 3 attempts"


def test_send_document_client_error_not_retried() -> None:
    handler = _Handler(httpx.Response(403))
    with pytest.raises(UpstreamFailed) as info:
        _notifier(handler, []).send_document(CHAT, "report.html", REPORT)
    assert len(handler.requests) == 1 and info.value.detail == "rejected: HTTP 403"


def test_send_document_telegram_down_fault_and_missing_token() -> None:
    handler = _Handler(_ok())
    client = httpx.Client(transport=httpx.MockTransport(handler))
    down = TelegramNotifier(
        _settings(), client=client, sleep=lambda _: None, fault=Fault.TELEGRAM_DOWN
    )
    with pytest.raises(UpstreamFailed, match="down after 3 attempts"):
        down.send_document(CHAT, "report.html", REPORT)
    no_token = TelegramNotifier(Settings(_env_file=None), client=client, sleep=lambda _: None)
    with pytest.raises(UpstreamFailed):
        no_token.send_document(CHAT, "report.html", REPORT)
    assert handler.requests == []


def test_send_document_token_never_in_error_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    def leaky(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    for handler in (_Handler(_ok()), leaky):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        notifier = TelegramNotifier(_settings(), client=client, sleep=lambda _: None)
        try:
            notifier.send_document(CHAT, "report.html", REPORT)
        except UpstreamFailed as exc:
            assert exc.__cause__ is None
            assert TOKEN not in "".join(traceback.format_exception(exc))
    assert caplog.records and TOKEN not in caplog.text
