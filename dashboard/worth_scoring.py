"""Scoring helpers for prioritising posts with under-collected comments."""

from typing import Any, Dict


def score_post(post: Dict[str, Any]) -> Dict[str, Any]:
    """Return a transparent score driven by the local comment collection gap."""
    likes = max(0, int(post.get("liked_count") or 0))
    platform_comments = max(0, int(post.get("comment_count") or 0))
    saved_comments = max(0, int(post.get("db_comment_count") or 0))
    desc_length = max(0, int(post.get("desc_length") or 0))
    title = (post.get("title") or "").lower()

    comment_gap = max(0, platform_comments - saved_comments)
    coverage_rate = (
        min(1.0, saved_comments / platform_comments)
        if platform_comments
        else 1.0
    )

    score = 0

    # The primary signal: how many known comments remain uncollected.
    if comment_gap >= 1000:
        score += 35
    elif comment_gap >= 300:
        score += 30
    elif comment_gap >= 100:
        score += 25
    elif comment_gap >= 50:
        score += 18
    elif comment_gap >= 20:
        score += 10

    # Prefer posts where the local sample is least representative.
    if platform_comments >= 20:
        if coverage_rate < 0.01:
            score += 25
        elif coverage_rate < 0.05:
            score += 20
        elif coverage_rate < 0.20:
            score += 12
        elif coverage_rate < 0.50:
            score += 5

    if likes >= 5000:
        score += 20
    elif likes >= 1000:
        score += 15
    elif likes >= 500:
        score += 8
    elif likes >= 100:
        score += 3

    if platform_comments >= 500:
        score += 10
    elif platform_comments >= 100:
        score += 7
    elif platform_comments >= 20:
        score += 3

    if desc_length >= 500:
        score += 5
    elif desc_length >= 200:
        score += 3

    if any(w in title for w in ("怎么", "如何", "为什么", "怎么办", "求", "推荐")):
        score += 5
    if any(w in title for w in ("难受", "痛苦", "不舒服", "问题", "缺点", "想要", "希望", "建议", "功能")):
        score += 5

    if any(w in title for w in ("教程", "攻略", "方法", "技巧", "app", "软件", "工具", "ai生成", "剪辑")):
        score -= 10
    if any(w in title for w in ("广告", "推广", "合作", "招募", "私信", "下单", "购买", "链接")):
        score -= 10

    return {
        "worth_score": max(0, score),
        "comment_gap": comment_gap,
        "coverage_rate": round(coverage_rate, 4),
    }
