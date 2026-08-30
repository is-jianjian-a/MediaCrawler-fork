"""Account-level XHS risk policy state-machine tests (no network/browser)."""

from dashboard import risk_policy as policy


def _temp_policy(monkeypatch, tmp_path):
    monkeypatch.setattr(policy, "RISK_DB", str(tmp_path / "risk.db"))
    policy.init_risk_policy_db()


def _search_config(**overrides):
    config = {
        "user_data_dir": "%s_user_data_dir_account02",
        "max_count": 14,
        "get_comments": False,
        "max_concurrency": 1,
        "min_sleep": 45,
        "max_sleep": 65,
        "enable_cdp": False,
        "require_cdp": False,
        "browser_path": "/isolated/chromium",
    }
    config.update(overrides)
    return config


def test_risk_cooldown_then_two_clean_canaries(monkeypatch, tmp_path):
    _temp_policy(monkeypatch, tmp_path)
    start = 1_800_000_000.0

    first = policy.reserve_launch(
        task_id="search-risk", task_kind="search", config=_search_config(), now=start
    )
    assert first.allowed and first.canary
    policy.confirm_launch("search-risk", "%s_user_data_dir_account02", now=start)
    status = policy.record_completion(
        task_id="search-risk",
        task_kind="search",
        user_data_dir="%s_user_data_dir_account02",
        exit_code=75,
        now=start + 10,
    )
    assert status["state"] == "cooldown"
    assert status["clean_canaries"] == 0

    denied = policy.reserve_launch(
        task_id="too-soon", task_kind="search", config=_search_config(), now=start + 100
    )
    assert not denied.allowed
    assert denied.state == "cooldown"

    canary_one_at = start + 10 + policy.COOLDOWN_SECONDS
    canary_one = policy.reserve_launch(
        task_id="canary-1", task_kind="search", config=_search_config(), now=canary_one_at
    )
    assert canary_one.allowed and canary_one.canary
    policy.confirm_launch("canary-1", "%s_user_data_dir_account02", now=canary_one_at)
    status = policy.record_completion(
        task_id="canary-1",
        task_kind="search",
        user_data_dir="%s_user_data_dir_account02",
        exit_code=0,
        now=canary_one_at + 10,
    )
    assert status["state"] == "canary"
    assert status["clean_canaries"] == 1

    canary_two_at = canary_one_at + 10 + policy.BROWSER_GAP_SECONDS
    canary_two = policy.reserve_launch(
        task_id="canary-2", task_kind="search", config=_search_config(), now=canary_two_at
    )
    assert canary_two.allowed and canary_two.canary
    policy.confirm_launch("canary-2", "%s_user_data_dir_account02", now=canary_two_at)
    status = policy.record_completion(
        task_id="canary-2",
        task_kind="search",
        user_data_dir="%s_user_data_dir_account02",
        exit_code=0,
        now=canary_two_at + 10,
    )
    assert status["state"] == "normal"
    assert status["clean_canaries"] == 2


def test_second_risk_same_day_locks_until_next_day(monkeypatch, tmp_path):
    _temp_policy(monkeypatch, tmp_path)
    first_risk = 1_800_000_000.0
    policy.record_completion(
        task_id="risk-1", task_kind="search",
        user_data_dir="%s_user_data_dir_account02", exit_code=75, now=first_risk,
    )
    status = policy.record_completion(
        task_id="risk-2", task_kind="search",
        user_data_dir="%s_user_data_dir_account02", exit_code=75, now=first_risk + 3600,
    )
    assert status["state"] == "locked"
    assert status["risk_count_day"] == 2
    assert status["locked_until"] > first_risk + 3600


def test_comments_require_normal_state_and_small_scope(monkeypatch, tmp_path):
    _temp_policy(monkeypatch, tmp_path)
    now = 1_800_000_000.0
    policy.record_completion(
        task_id="risk", task_kind="search",
        user_data_dir="%s_user_data_dir_account02", exit_code=75, now=now,
    )
    blocked = policy.reserve_launch(
        task_id="comments", task_kind="comment",
        total_posts=1,
        config=_search_config(max_comments=5, comment_sleep=90),
        now=now + policy.COOLDOWN_SECONDS,
    )
    assert not blocked.allowed
    assert "两次干净搜索" in blocked.reason

    # A fresh account cannot start comments before two clean search canaries.
    fresh = policy.reserve_launch(
        task_id="comments-fresh", task_kind="comment", total_posts=1,
        config=_search_config(user_data_dir="other", max_comments=5, comment_sleep=90),
        now=now,
    )
    assert not fresh.allowed
    assert "两次干净搜索" in fresh.reason


def test_comment_sessions_are_batched_after_two_search_sessions(monkeypatch, tmp_path):
    _temp_policy(monkeypatch, tmp_path)
    monkeypatch.setattr(policy, "BROWSER_GAP_SECONDS", 0)
    monkeypatch.setattr(policy, "TASK_GAP_SECONDS", 0)
    now = 1_800_000_000.0
    comment_config = _search_config(max_comments=5, comment_sleep=90)

    blocked = policy.reserve_launch(
        task_id="comments-early",
        task_kind="comment",
        total_posts=2,
        config=comment_config,
        now=now,
    )
    assert not blocked.allowed
    assert "两次干净搜索" in blocked.reason

    for index in range(2):
        task_id = f"search-{index}"
        at = now + index * 10 + 1
        decision = policy.reserve_launch(
            task_id=task_id,
            task_kind="search",
            config=_search_config(),
            now=at,
        )
        assert decision.allowed
        policy.confirm_launch(task_id, "%s_user_data_dir_account02", now=at)
        policy.record_completion(
            task_id=task_id,
            task_kind="search",
            user_data_dir="%s_user_data_dir_account02",
            exit_code=0,
            now=at + 1,
        )

    allowed = policy.reserve_launch(
        task_id="comments-batched",
        task_kind="comment",
        total_posts=2,
        config=comment_config,
        now=now + 30,
    )
    assert allowed.allowed


def test_abort_releases_atomic_reservation(monkeypatch, tmp_path):
    _temp_policy(monkeypatch, tmp_path)
    now = 1_800_000_000.0
    decision = policy.reserve_launch(
        task_id="first", task_kind="search", config=_search_config(), now=now
    )
    assert decision.allowed
    policy.abort_launch("first", "%s_user_data_dir_account02", "spawn failed")
    replacement = policy.reserve_launch(
        task_id="replacement", task_kind="search", config=_search_config(), now=now + 1
    )
    assert replacement.allowed


def test_running_lease_is_renewed_and_blocks_same_account(monkeypatch, tmp_path):
    _temp_policy(monkeypatch, tmp_path)
    monkeypatch.setattr(policy, "BROWSER_GAP_SECONDS", 0)
    monkeypatch.setattr(policy, "TASK_GAP_SECONDS", 0)
    monkeypatch.setattr(policy, "RUNNING_LEASE_TTL_SECONDS", 120)
    now = 1_800_000_000.0

    first = policy.reserve_launch(
        task_id="owner", task_kind="search", config=_search_config(), now=now
    )
    assert first.allowed
    policy.confirm_launch("owner", "%s_user_data_dir_account02", now=now)
    assert policy.heartbeat_launch(
        "owner", "%s_user_data_dir_account02", now=now + 100
    )

    blocked = policy.reserve_launch(
        task_id="racer",
        task_kind="search",
        config=_search_config(),
        now=now + 150,
    )
    assert not blocked.allowed
    assert "已有任务" in blocked.reason
    assert policy.get_status(now=now + 150)["lease_expires_at"] == now + 220


def test_late_completion_cannot_release_new_owner(monkeypatch, tmp_path):
    _temp_policy(monkeypatch, tmp_path)
    monkeypatch.setattr(policy, "BROWSER_GAP_SECONDS", 0)
    monkeypatch.setattr(policy, "TASK_GAP_SECONDS", 0)
    monkeypatch.setattr(policy, "RUNNING_LEASE_TTL_SECONDS", 10)
    now = 1_800_000_000.0

    assert policy.reserve_launch(
        task_id="old", task_kind="search", config=_search_config(), now=now
    ).allowed
    policy.confirm_launch("old", "%s_user_data_dir_account02", now=now)
    assert policy.reserve_launch(
        task_id="new", task_kind="search", config=_search_config(), now=now + 11
    ).allowed
    policy.confirm_launch("new", "%s_user_data_dir_account02", now=now + 11)

    status = policy.record_completion(
        task_id="old",
        task_kind="search",
        user_data_dir="%s_user_data_dir_account02",
        exit_code=0,
        now=now + 12,
    )
    assert status["active_task_id"] == "new"
    assert status["lease_expires_at"] == now + 21


def test_risk_control_isolated_between_profiles(monkeypatch, tmp_path):
    _temp_policy(monkeypatch, tmp_path)
    now = 1_800_000_000.0
    profile_a = "%s_user_data_dir_accountA"
    profile_b = "%s_user_data_dir_accountB"

    policy.record_completion(
        task_id="risk-a",
        task_kind="search",
        user_data_dir=profile_a,
        exit_code=75,
        now=now,
    )
    assert policy.get_status(profile_a, now=now + 1)["state"] == "cooldown"
    assert policy.get_status(profile_b, now=now + 1)["state"] == "canary"
    decision = policy.reserve_launch(
        task_id="search-b",
        task_kind="search",
        config=_search_config(user_data_dir=profile_b),
        now=now + 1,
    )
    assert decision.allowed and decision.canary


def test_launch_budget_retry_waits_until_count_falls_below_limit(monkeypatch, tmp_path):
    _temp_policy(monkeypatch, tmp_path)
    monkeypatch.setattr(policy, "BROWSER_GAP_SECONDS", 0)
    monkeypatch.setattr(policy, "TASK_GAP_SECONDS", 0)
    start = 1_800_000_000.0
    for index in range(policy.MAX_LAUNCHES_PER_WINDOW):
        task_id = f"launch-{index}"
        at = start + index * 10
        decision = policy.reserve_launch(
            task_id=task_id, task_kind="search", config=_search_config(), now=at
        )
        assert decision.allowed
        policy.confirm_launch(task_id, "%s_user_data_dir_account02", now=at)
        policy.record_completion(
            task_id=task_id, task_kind="search",
            user_data_dir="%s_user_data_dir_account02", exit_code=0, now=at + 1,
        )

    status = policy.get_status(now=start + 100)
    assert status["launches_12h"] == policy.MAX_LAUNCHES_PER_WINDOW
    # At the configured limit, the oldest launch must leave the rolling
    # window before another launch is allowed.
    assert status["launch_budget_retry_at"] == start + policy.LAUNCH_WINDOW_SECONDS
