"""Regression tests for conservative Xiaohongshu crawl-rate policy."""

from dashboard.rate_policy import (
    COMMENTS_RATE,
    DEFAULT_COMMENT_TASK_BATCH_DELAY,
    DEFAULT_COMMENT_TASK_BATCH_SIZE,
    DEFAULT_FIRST_LEVEL_COMMENTS,
    POST_ONLY_RATE,
    keyword_rate_defaults,
)


def test_post_only_keyword_defaults_are_conservative():
    assert keyword_rate_defaults(False) == {
        "min_sleep": 45,
        "max_sleep": 65,
        "comment_sleep": 30,
        "max_concurrency": 1,
    }
    assert POST_ONLY_RATE.as_dict() == keyword_rate_defaults(False)


def test_keyword_comment_defaults_are_slower_than_post_only():
    assert keyword_rate_defaults(True) == {
        "min_sleep": 60,
        "max_sleep": 75,
        "comment_sleep": 90,
        "max_concurrency": 1,
    }
    assert COMMENTS_RATE.as_dict() == keyword_rate_defaults(True)


def test_comment_task_safety_constants():
    assert DEFAULT_FIRST_LEVEL_COMMENTS == 5
    assert DEFAULT_COMMENT_TASK_BATCH_SIZE == 10
    assert DEFAULT_COMMENT_TASK_BATCH_DELAY == 60
