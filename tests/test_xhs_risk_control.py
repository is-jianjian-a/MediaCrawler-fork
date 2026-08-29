"""No-network regression tests for Xiaohongshu risk-control handling."""

import asyncio

import pytest

import config
from media_platform.xhs import client as xhs_client_module
from media_platform.xhs import core as xhs_core_module
from media_platform.xhs.client import XiaoHongShuClient
from media_platform.xhs.exception import RiskControlError


class _FakeResponse:
    def __init__(self, status_code: int, headers=None):
        self.status_code = status_code
        self.headers = headers if headers is not None else {
            "Verifytype": "slider",
            "Verifyuuid": "test-uuid",
        }
        self.text = "captcha"

    def json(self):  # pragma: no cover - a risk response must not be decoded
        raise AssertionError("risk-control responses should fail before JSON decoding")


class _FakeHttpClient:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    async def request(self, *args, **kwargs):
        self.calls += 1
        return self.response


class _FakeClientContext:
    def __init__(self, http_client):
        self.http_client = http_client

    async def __aenter__(self):
        return self.http_client

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _bare_client() -> XiaoHongShuClient:
    client = XiaoHongShuClient.__new__(XiaoHongShuClient)
    client.proxy = None
    client.timeout = 1
    client._proxy_ip_pool = None
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [461, 471])
async def test_request_fails_once_for_captcha_status(monkeypatch, status_code):
    http_client = _FakeHttpClient(_FakeResponse(status_code))
    monkeypatch.setattr(
        xhs_client_module,
        "make_async_client",
        lambda **kwargs: _FakeClientContext(http_client),
    )

    with pytest.raises(RiskControlError):
        await _bare_client().request("GET", "https://example.test/api")

    # Risk-control errors are terminal for this request; tenacity must not retry.
    assert http_client.calls == 1


@pytest.mark.asyncio
async def test_request_fails_fast_without_captcha_headers(monkeypatch):
    """Malformed CAPTCHA responses still use the typed terminal error."""
    http_client = _FakeHttpClient(_FakeResponse(461, headers={}))
    monkeypatch.setattr(
        xhs_client_module,
        "make_async_client",
        lambda **kwargs: _FakeClientContext(http_client),
    )

    with pytest.raises(RiskControlError):
        await _bare_client().request("GET", "https://example.test/api")

    assert http_client.calls == 1


@pytest.mark.asyncio
async def test_html_note_fetch_does_not_retry_risk_control(monkeypatch):
    client = _bare_client()
    client.headers = {"Cookie": "session"}
    client._domain = "https://example.test"
    calls = 0

    async def fail_request(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise RiskControlError("captcha")

    client.request = fail_request

    with pytest.raises(RiskControlError):
        await client.get_note_by_id_from_html(
            note_id="note-1",
            xsec_source="pc_search",
            xsec_token="token",
        )

    assert calls == 1


@pytest.mark.asyncio
async def test_core_get_comments_does_not_swallow_risk_control(monkeypatch):
    class FakeClient:
        comment_calls = 0

        async def get_note_by_id(self, **kwargs):
            raise RiskControlError("captcha")

        async def get_note_all_comments(self, **kwargs):
            self.comment_calls += 1

    crawler = xhs_core_module.XiaoHongShuCrawler.__new__(xhs_core_module.XiaoHongShuCrawler)
    crawler.xhs_client = FakeClient()
    monkeypatch.setattr(config, "CRAWLER_COMMENT_SLEEP_SEC", 0, raising=False)

    with pytest.raises(RiskControlError):
        await crawler.get_comments(
            note_id="note-1",
            xsec_token="token",
            semaphore=asyncio.Semaphore(1),
        )

    assert crawler.xhs_client.comment_calls == 0


@pytest.mark.asyncio
async def test_sub_comments_does_not_swallow_risk_control(monkeypatch):
    client = _bare_client()

    async def fail_sub_comments(**kwargs):
        raise RiskControlError("captcha")

    client.get_note_sub_comments = fail_sub_comments
    monkeypatch.setattr(config, "ENABLE_GET_SUB_COMMENTS", True)
    monkeypatch.setattr(config, "CRAWLER_MAX_SUB_COMMENTS_COUNT_SINGLENOTES", 10)

    with pytest.raises(RiskControlError):
        await client.get_comments_all_sub_comments(
            comments=[
                {
                    "id": "root-1",
                    "note_id": "note-1",
                    "sub_comment_has_more": True,
                    "sub_comment_cursor": "cursor-1",
                }
            ],
            xsec_token="token",
        )


@pytest.mark.asyncio
async def test_risk_control_cancels_sibling_requests():
    crawler = xhs_core_module.XiaoHongShuCrawler.__new__(xhs_core_module.XiaoHongShuCrawler)
    sibling_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()

    async def pending_request():
        sibling_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            sibling_cancelled.set()

    async def risk_request():
        await sibling_started.wait()
        raise RiskControlError("captcha")

    with pytest.raises(RiskControlError):
        await crawler._gather_cancel_on_risk([pending_request(), risk_request()])

    assert sibling_cancelled.is_set()
