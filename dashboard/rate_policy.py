"""Evidence-backed Xiaohongshu crawl-rate defaults used by Dashboard tasks.

These values are operating defaults, not a claimed platform rate limit.  Keep
the policy in one small module so task creation and task execution cannot drift
apart again.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class XhsRateProfile:
    min_sleep: int
    max_sleep: int
    comment_sleep: int
    max_concurrency: int = 1

    def as_dict(self) -> dict[str, int]:
        return {
            "min_sleep": self.min_sleep,
            "max_sleep": self.max_sleep,
            "comment_sleep": self.comment_sleep,
            "max_concurrency": self.max_concurrency,
        }


# Search/detail only: the historical 45-65 second group was the most stable
# usable baseline for long-running keyword tasks.
POST_ONLY_RATE = XhsRateProfile(
    min_sleep=45,
    max_sleep=65,
    comment_sleep=30,
)

# Search/detail plus first-level comments, and standalone comment supplement
# tasks: comment work adds a detail refresh and one or more paginated requests,
# so it uses a slower detail cadence and a much longer inter-note pause.
COMMENTS_RATE = XhsRateProfile(
    min_sleep=60,
    max_sleep=75,
    comment_sleep=90,
)

DEFAULT_FIRST_LEVEL_COMMENTS = 5
DEFAULT_COMMENT_TASK_BATCH_SIZE = 10
DEFAULT_COMMENT_TASK_BATCH_DELAY = 60


def keyword_rate_defaults(get_comments: bool) -> dict[str, int]:
    """Return the conservative keyword-task profile for the selected scope."""
    return (COMMENTS_RATE if get_comments else POST_ONLY_RATE).as_dict()
