import asyncio
from types import SimpleNamespace

import pytest

import config
from media_platform.xhs import client as xhs_client_module
from media_platform.xhs import core as xhs_core_module
from media_platform.xhs.client import XiaoHongShuClient
from media_platform.xhs.core import XiaoHongShuCrawler


def _patch_async(monkeypatch, target, name, func):
    monkeypatch.setattr(target, name, func)


def _note(note_id: str) -> dict:
    return {
        "note_id": note_id,
        "title": note_id,
        "desc": "detail",
        "time": 0,
        "user": {},
        "interact_info": {},
        "image_list": [],
        "tag_list": [],
    }


@pytest.mark.asyncio
async def test_search_persists_first_detail_before_post_fetch_sleep(monkeypatch):
    crawler = XiaoHongShuCrawler()
    persisted = []
    persisted_event = asyncio.Event()
    sleep_started = asyncio.Event()
    release_sleep = asyncio.Event()

    class FakeClient:
        async def get_note_by_keyword(self, **kwargs):
            return {
                "has_more": False,
                "items": [
                    {"id": "note-1", "xsec_source": "pc_search", "xsec_token": "token-1"},
                    {"id": "note-2", "xsec_source": "pc_search", "xsec_token": "token-2"},
                ],
            }

        async def get_note_by_id(self, note_id, xsec_source, xsec_token):
            return _note(note_id)

    async def fake_update(note_detail):
        persisted.append(note_detail["note_id"])
        persisted_event.set()

    async def blocking_sleep(*args, **kwargs):
        sleep_started.set()
        await release_sleep.wait()

    async def no_op(*args, **kwargs):
        return None

    async def should_process_keyword(keyword):
        return 2, set()

    crawler.xhs_client = FakeClient()
    crawler._filter_search_items = lambda items, existing_ids: items
    crawler._should_skip_keyword = should_process_keyword
    crawler.get_notice_media = no_op

    monkeypatch.setattr(config, "KEYWORDS", "test-keyword")
    monkeypatch.setattr(config, "START_PAGE", 1)
    monkeypatch.setattr(config, "SORT_TYPE", "")
    monkeypatch.setattr(config, "NOTE_TYPE", "all")
    monkeypatch.setattr(config, "MAX_CONCURRENCY_NUM", 1)
    monkeypatch.setattr(config, "CRAWLER_MAX_NOTES_COUNT", 2)
    monkeypatch.setattr(config, "XHS_SEARCH_MAX_ITEMS", 2)
    monkeypatch.setattr(config, "XHS_STOP_WHEN_BEFORE_DATE", False)
    monkeypatch.setattr(config, "XHS_NOTE_PUBLISH_DATE_AFTER", "")
    monkeypatch.setattr(config, "ENABLE_TEST_MODE", False)
    monkeypatch.setattr(xhs_core_module, "smart_sleep", blocking_sleep)
    _patch_async(monkeypatch, xhs_core_module.xhs_store, "update_xhs_note", fake_update)
    _patch_async(monkeypatch, xhs_core_module.xhs_store, "record_xhs_note_keyword_hit", no_op)

    task = asyncio.create_task(crawler.search())
    await asyncio.wait_for(persisted_event.wait(), timeout=1)
    await asyncio.wait_for(sleep_started.wait(), timeout=1)

    assert persisted == ["note-1"]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_specified_notes_persist_first_detail_before_batch_finishes(monkeypatch):
    crawler = XiaoHongShuCrawler()
    persisted = []
    persisted_event = asyncio.Event()
    sleep_started = asyncio.Event()
    release_sleep = asyncio.Event()

    class FakeClient:
        async def get_note_by_id(self, note_id, xsec_source, xsec_token):
            return _note(note_id)

    async def fake_update(note_detail):
        persisted.append(note_detail["note_id"])
        persisted_event.set()

    async def blocking_sleep(*args, **kwargs):
        sleep_started.set()
        await release_sleep.wait()

    async def no_op(*args, **kwargs):
        return None

    crawler.xhs_client = FakeClient()
    crawler.get_notice_media = no_op
    crawler.batch_get_note_comments = no_op

    monkeypatch.setattr(config, "XHS_SPECIFIED_NOTE_URL_LIST", ["url-1", "url-2"])
    monkeypatch.setattr(config, "MAX_CONCURRENCY_NUM", 1)
    monkeypatch.setattr(config, "XHS_NOTE_PUBLISH_DATE_AFTER", "")
    monkeypatch.setattr(
        xhs_core_module,
        "parse_note_info_from_note_url",
        lambda url: SimpleNamespace(
            note_id="note-1" if url == "url-1" else "note-2",
            xsec_source="pc_search",
            xsec_token="token",
        ),
    )
    monkeypatch.setattr(xhs_core_module, "smart_sleep", blocking_sleep)
    _patch_async(monkeypatch, xhs_core_module.xhs_store, "update_xhs_note", fake_update)

    task = asyncio.create_task(crawler.get_specified_notes())
    await asyncio.wait_for(persisted_event.wait(), timeout=1)
    await asyncio.wait_for(sleep_started.wait(), timeout=1)

    assert persisted == ["note-1"]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_creator_batch_persists_first_detail_before_batch_finishes(monkeypatch):
    crawler = XiaoHongShuCrawler()
    persisted = []
    persisted_event = asyncio.Event()
    sleep_started = asyncio.Event()
    release_sleep = asyncio.Event()

    class FakeClient:
        async def get_note_by_id(self, note_id, xsec_source, xsec_token):
            return _note(note_id)

    async def fake_update(note_detail):
        persisted.append(note_detail["note_id"])
        persisted_event.set()

    async def blocking_sleep(*args, **kwargs):
        sleep_started.set()
        await release_sleep.wait()

    async def no_op(*args, **kwargs):
        return None

    crawler.xhs_client = FakeClient()
    crawler.get_notice_media = no_op

    monkeypatch.setattr(config, "MAX_CONCURRENCY_NUM", 1)
    monkeypatch.setattr(config, "XHS_NOTE_PUBLISH_DATE_AFTER", "")
    monkeypatch.setattr(xhs_core_module, "smart_sleep", blocking_sleep)
    _patch_async(monkeypatch, xhs_core_module.xhs_store, "update_xhs_note", fake_update)

    task = asyncio.create_task(
        crawler.fetch_creator_notes_detail(
            [
                {"note_id": "note-1", "xsec_source": "pc_feed", "xsec_token": "token-1"},
                {"note_id": "note-2", "xsec_source": "pc_feed", "xsec_token": "token-2"},
            ]
        )
    )
    await asyncio.wait_for(persisted_event.wait(), timeout=1)
    await asyncio.wait_for(sleep_started.wait(), timeout=1)

    assert persisted == ["note-1"]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_comment_and_subcomment_pages_persist_before_page_sleep(monkeypatch):
    client = XiaoHongShuClient.__new__(XiaoHongShuClient)
    persisted_batches = []
    sleep_started = asyncio.Event()
    release_sleep = asyncio.Event()

    root_comment = {
        "id": "root-1",
        "note_id": "note-1",
        "sub_comments": [{"id": "sub-embedded", "note_id": "note-1"}],
        "sub_comment_has_more": True,
        "sub_comment_cursor": "cursor-1",
    }

    async def get_root_page(**kwargs):
        return {"has_more": False, "cursor": "", "comments": [root_comment]}

    async def get_sub_page(**kwargs):
        return {
            "has_more": False,
            "cursor": "",
            "comments": [{"id": "sub-page-1", "note_id": "note-1"}],
        }

    async def persist_page(note_id, comments):
        persisted_batches.append((note_id, [comment["id"] for comment in comments]))

    async def blocking_sleep(*args, **kwargs):
        sleep_started.set()
        await release_sleep.wait()

    client.get_note_comments = get_root_page
    client.get_note_sub_comments = get_sub_page
    monkeypatch.setattr(config, "ENABLE_GET_SUB_COMMENTS", True)
    monkeypatch.setattr(config, "CRAWLER_MAX_SUB_COMMENTS_COUNT_SINGLENOTES", 10)
    monkeypatch.setattr(xhs_client_module.asyncio, "sleep", blocking_sleep)

    task = asyncio.create_task(
        client.get_note_all_comments(
            note_id="note-1",
            xsec_token="token",
            callback=persist_page,
            max_count=10,
        )
    )
    await asyncio.wait_for(sleep_started.wait(), timeout=1)

    assert persisted_batches == [
        ("note-1", ["root-1"]),
        ("note-1", ["sub-embedded"]),
        ("note-1", ["sub-page-1"]),
    ]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_search_summary_counts_only_real_detail_and_comment_requests(monkeypatch):
    crawler = XiaoHongShuCrawler()
    detail_requests = []
    summaries = []

    class FakeClient:
        async def get_note_by_keyword(self, **kwargs):
            return {
                "has_more": False,
                "items": [
                    {
                        "id": "existing-note",
                        "xsec_source": "pc_search",
                        "xsec_token": "existing-token",
                        "_skip_detail": True,
                    },
                    {
                        "id": "new-note",
                        "xsec_source": "pc_search",
                        "xsec_token": "new-token",
                    },
                ],
            }

        async def get_note_by_id(self, note_id, xsec_source, xsec_token):
            detail_requests.append(note_id)
            return _note(note_id)

    async def no_op(*args, **kwargs):
        return None

    async def should_process_keyword(keyword):
        return 1, {"existing-note"}

    crawler.xhs_client = FakeClient()
    crawler._filter_search_items = lambda items, existing_ids: items
    crawler._should_skip_keyword = should_process_keyword
    crawler.get_notice_media = no_op
    crawler.batch_get_note_comments = no_op
    crawler._log_search_summary = lambda *args: summaries.append(args)

    monkeypatch.setattr(config, "KEYWORDS", "test-keyword")
    monkeypatch.setattr(config, "START_PAGE", 1)
    monkeypatch.setattr(config, "SORT_TYPE", "")
    monkeypatch.setattr(config, "NOTE_TYPE", "all")
    monkeypatch.setattr(config, "MAX_CONCURRENCY_NUM", 1)
    monkeypatch.setattr(config, "CRAWLER_MAX_NOTES_COUNT", 1)
    monkeypatch.setattr(config, "XHS_SEARCH_MAX_ITEMS", 2)
    monkeypatch.setattr(config, "XHS_STOP_WHEN_BEFORE_DATE", False)
    monkeypatch.setattr(config, "XHS_NOTE_PUBLISH_DATE_AFTER", "")
    monkeypatch.setattr(config, "ENABLE_GET_COMMENTS", False)
    monkeypatch.setattr(config, "ENABLE_TEST_MODE", False)
    monkeypatch.setattr(xhs_core_module, "smart_sleep", no_op)
    _patch_async(monkeypatch, xhs_core_module.xhs_store, "update_xhs_note", no_op)
    _patch_async(monkeypatch, xhs_core_module.xhs_store, "record_xhs_note_keyword_hit", no_op)

    await crawler.search()

    assert detail_requests == ["new-note"]
    assert len(summaries) == 1
    assert summaries[0][7] == 1
    assert summaries[0][8] == 0


@pytest.mark.asyncio
async def test_specified_notes_process_detail_then_comments_one_note_at_a_time(monkeypatch):
    crawler = XiaoHongShuCrawler()
    events = []

    async def fetch_detail(
        note_id, xsec_source, xsec_token, semaphore, skip_detail=False,
        detail_callback=None,
    ):
        events.append(f"detail:{note_id}")
        detail = _note(note_id)
        detail.update({"xsec_token": xsec_token, "xsec_source": xsec_source})
        if detail_callback:
            await detail_callback(detail)
        return detail

    async def persist_detail(detail):
        events.append(f"persist-detail:{detail['note_id']}")
        return True

    async def fetch_comments(note_ids, tokens):
        note_id = note_ids[0]
        events.append(f"comments:{note_id}")
        events.append(f"persist-comments:{note_id}")

    monkeypatch.setattr(config, "MAX_CONCURRENCY_NUM", 1)
    monkeypatch.setattr(config, "XHS_INTER_NOTE_SLEEP_SEC", 0)
    monkeypatch.setattr(config, "XHS_SPECIFIED_NOTE_URL_LIST", ["url-1", "url-2"])
    monkeypatch.setattr(
        xhs_core_module,
        "parse_note_info_from_note_url",
        lambda url: SimpleNamespace(
            note_id="note-1" if url == "url-1" else "note-2",
            xsec_source="pc_search",
            xsec_token="token",
        ),
    )
    crawler.get_note_detail_async_task = fetch_detail
    crawler._persist_fetched_note_detail = persist_detail
    crawler.batch_get_note_comments = fetch_comments

    await crawler.get_specified_notes()

    assert events == [
        "detail:note-1",
        "persist-detail:note-1",
        "comments:note-1",
        "persist-comments:note-1",
        "detail:note-2",
        "persist-detail:note-2",
        "comments:note-2",
        "persist-comments:note-2",
    ]
