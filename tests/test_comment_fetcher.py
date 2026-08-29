import json
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dashboard import comment_fetcher
from dashboard import server as dashboard_server
from dashboard.comment_fetcher import CommentTaskExecutor
from tools.app_runner import RISK_CONTROL_EXIT_CODE
from dashboard.risk_policy import LaunchDecision


class CommentFetcherTests(unittest.TestCase):
    @staticmethod
    def _make_browser_executable(temp_dir: str) -> str:
        browser_path = Path(temp_dir) / "isolated-chromium"
        browser_path.touch()
        browser_path.chmod(0o755)
        return str(browser_path)

    def test_detail_url_keeps_token_and_source(self):
        url = CommentTaskExecutor.detail_url({
            "note_id": "abc",
            "note_url": "https://www.xiaohongshu.com/explore/abc",
            "xsec_token": "token-value",
        })
        self.assertIn("xsec_token=token-value", url)
        self.assertIn("xsec_source=pc_search", url)

    def test_detail_url_rejects_missing_token(self):
        with self.assertRaisesRegex(ValueError, "xsec_token"):
            CommentTaskExecutor.detail_url({"note_id": "abc", "note_url": ""})

    def test_default_environment_uses_safe_comment_task_rate(self):
        """Comment supplement tasks must launch with the conservative rate profile."""
        rate_keys = (
            "MEDIACRAWLER_CRAWLER_MIN_SLEEP_SEC",
            "MEDIACRAWLER_CRAWLER_MAX_SLEEP_SEC",
            "MEDIACRAWLER_CRAWLER_COMMENT_SLEEP_SEC",
            "MEDIACRAWLER_MAX_CONCURRENCY_NUM",
        )
        # Make the assertion independent of a developer's shell environment.
        with mock.patch.dict(os.environ, {}, clear=True):
            with tempfile.TemporaryDirectory() as temp_dir:
                browser_path = self._make_browser_executable(temp_dir)
                executor = CommentTaskExecutor(
                    os.path.join(temp_dir, "comments.db"),
                    max_comments=1,
                    dry_run=True,
                    browser_path=browser_path,
                )
                try:
                    env = executor.crawler_environment()
                finally:
                    executor.close()

        self.assertEqual(env[rate_keys[0]], "60")
        self.assertEqual(env[rate_keys[1]], "75")
        self.assertEqual(env[rate_keys[2]], "90")
        self.assertEqual(env[rate_keys[3]], "1")
        self.assertEqual(env["MEDIACRAWLER_BROWSER_PATH"], str(Path(browser_path).resolve()))

    def test_standard_environment_overrides_inherited_browser_with_isolated_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            browser_path = self._make_browser_executable(temp_dir)
            with mock.patch.dict(
                os.environ,
                {
                    "MEDIACRAWLER_ENABLE_CDP": "true",
                    "MEDIACRAWLER_REQUIRE_CDP": "true",
                    "MEDIACRAWLER_CDP_ENDPOINT": "http://127.0.0.1:9222",
                    "MEDIACRAWLER_BROWSER_PATH": str(comment_fetcher.SYSTEM_CHROME_PATH),
                    "MEDIACRAWLER_XHS_NOTE_PUBLISH_DATE_AFTER": "2026-06-10",
                },
                clear=True,
            ):
                executor = CommentTaskExecutor(
                    os.path.join(temp_dir, "comments.db"),
                    max_comments=1,
                    dry_run=True,
                    browser_path=browser_path,
                )
                try:
                    env = executor.crawler_environment()
                finally:
                    executor.close()

        self.assertEqual(env["MEDIACRAWLER_BROWSER_PATH"], str(Path(browser_path).resolve()))
        self.assertEqual(env["MEDIACRAWLER_ENABLE_CDP"], "false")
        self.assertEqual(env["MEDIACRAWLER_REQUIRE_CDP"], "false")
        self.assertEqual(env["MEDIACRAWLER_CDP_CONNECT_EXISTING"], "false")
        self.assertNotIn("MEDIACRAWLER_CDP_ENDPOINT", env)
        self.assertEqual(
            env["MEDIACRAWLER_XHS_NOTE_PUBLISH_DATE_AFTER"],
            comment_fetcher.DEFAULT_COMMENT_PUBLISH_DATE_AFTER,
        )

    def test_standard_environment_never_falls_back_to_system_chrome(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.dict(
                os.environ,
                {"MEDIACRAWLER_BROWSER_PATH": str(comment_fetcher.SYSTEM_CHROME_PATH)},
                clear=True,
            ):
                executor = CommentTaskExecutor(
                    os.path.join(temp_dir, "comments.db"),
                    max_comments=1,
                    dry_run=True,
                )
                try:
                    with self.assertRaisesRegex(ValueError, "explicit isolated browser_path"):
                        executor.crawler_environment()
                finally:
                    executor.close()

    def test_dashboard_comment_task_preserves_and_launches_isolated_browser_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            browser_path = self._make_browser_executable(temp_dir)
            resolved_browser_path = str(Path(browser_path).resolve())
            content_db = os.path.join(temp_dir, "content.db")
            import sqlite3
            conn = sqlite3.connect(content_db)
            conn.execute(
                "CREATE TABLE xhs_note (note_id TEXT, crawler_account TEXT)"
            )
            conn.execute("INSERT INTO xhs_note VALUES ('note-1', 'test')")
            conn.commit()
            conn.close()
            account = {
                "account_id": "test",
                "user_data_dir": "%s_user_data_dir_accounttest",
                "browser_path": resolved_browser_path,
                "sqlite_db_path": content_db,
            }
            created_configs = []

            with (
                mock.patch.object(
                    dashboard_server,
                    "bind_task_config",
                    side_effect=lambda config, **kwargs: (
                        {**config, **account}, account
                    ),
                ),
                mock.patch.object(
                    dashboard_server,
                    "_with_crawler_db",
                    side_effect=lambda account_id="": sqlite3.connect(content_db),
                ),
                mock.patch(
                    "task_manager.create_task",
                    side_effect=lambda name, posts, config, group_tag="", account_id="": (
                        created_configs.append(config) or "task-browser"
                    ),
                ),
                dashboard_server.app.test_client() as client,
            ):
                response = client.post(
                    "/api/tasks",
                    json={
                        "name": "isolated-browser-task",
                        "posts": [{"note_id": "note-1"}],
                        "account_id": "test",
                        "config": {
                            "browser_mode": "standard",
                        },
                    },
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(created_configs[0]["browser_path"], resolved_browser_path)
            self.assertEqual(created_configs[0]["account_id"], "test")
            self.assertEqual(
                created_configs[0]["publish_date_after"],
                comment_fetcher.DEFAULT_COMMENT_PUBLISH_DATE_AFTER,
            )

            fake_worker = mock.Mock(pid=4321)
            with (
                dashboard_server.app.app_context(),
                mock.patch("task_manager.claim_task", return_value=(True, "")),
                mock.patch(
                    "task_manager.get_task",
                    return_value={
                        "config_json": json.dumps({
                            "account_id": "test",
                            "browser_mode": "standard",
                            "enable_cdp": False,
                            "require_cdp": False,
                            "browser_path": browser_path,
                        }),
                        "account_id": "test",
                    },
                ),
                mock.patch.object(
                    dashboard_server,
                    "bind_task_config",
                    return_value=({**account, "browser_mode": "standard"}, account),
                ),
                mock.patch.object(dashboard_server, "_account_has_active_task", return_value=False),
                mock.patch("task_manager.fail_task_start") as fail_start,
                mock.patch("task_manager.set_task_worker_pid"),
                mock.patch.object(
                    dashboard_server,
                    "reserve_launch",
                    return_value=LaunchDecision(True, "normal"),
                ),
                mock.patch.object(dashboard_server, "confirm_launch"),
                mock.patch.object(dashboard_server.subprocess, "Popen", return_value=fake_worker) as popen,
            ):
                response, status = dashboard_server._launch_comment_task("task-browser")

            self.assertEqual(status, 202)
            fail_start.assert_not_called()
            command = popen.call_args.args[0]
            env = popen.call_args.kwargs["env"]
            self.assertEqual(command[command.index("--browser-path") + 1], resolved_browser_path)
            self.assertEqual(
                command[command.index("--publish-date-after") + 1],
                comment_fetcher.DEFAULT_COMMENT_PUBLISH_DATE_AFTER,
            )
            self.assertEqual(env["MEDIACRAWLER_BROWSER_PATH"], resolved_browser_path)
            self.assertEqual(
                env["MEDIACRAWLER_XHS_NOTE_PUBLISH_DATE_AFTER"],
                comment_fetcher.DEFAULT_COMMENT_PUBLISH_DATE_AFTER,
            )

    def test_dashboard_rejects_invalid_comment_publish_date(self):
        with (
            mock.patch("task_manager.create_task") as create_task,
            dashboard_server.app.test_client() as client,
        ):
            response = client.post(
                "/api/tasks",
                json={
                    "name": "invalid-date-task",
                    "posts": [{"note_id": "note-1"}],
                    "config": {"publish_date_after": "not-a-date"},
                },
            )

        self.assertEqual(response.status_code, 400)
        create_task.assert_not_called()

    def test_dashboard_standard_task_without_browser_path_fails_before_spawn(self):
        account = {
            "account_id": "missing-browser",
            "user_data_dir": "%s_user_data_dir_missing_browser",
            "browser_path": "",
            "sqlite_db_path": "/tmp/missing-browser.db",
        }
        with (
            dashboard_server.app.app_context(),
            mock.patch("task_manager.claim_task", return_value=(True, "")),
            mock.patch(
                "task_manager.get_task",
                return_value={
                    "account_id": "missing-browser",
                    "config_json": json.dumps({
                        "account_id": "missing-browser",
                        "browser_mode": "standard",
                        "enable_cdp": False,
                        "require_cdp": False,
                    }),
                },
            ),
            mock.patch.object(
                dashboard_server,
                "bind_task_config",
                return_value=({**account, "browser_mode": "standard"}, account),
            ),
            mock.patch.object(dashboard_server, "_account_has_active_task", return_value=False),
            mock.patch("task_manager.fail_task_start") as fail_start,
            mock.patch("task_manager.set_task_worker_pid"),
            mock.patch.object(
                dashboard_server,
                "reserve_launch",
                return_value=LaunchDecision(True, "normal"),
            ),
            mock.patch.object(dashboard_server, "abort_launch"),
            mock.patch.object(dashboard_server.subprocess, "Popen") as popen,
        ):
            response, status = dashboard_server._launch_comment_task("task-no-browser")

        self.assertEqual(status, 500)
        self.assertIn("explicit isolated browser_path", response.get_json()["error"])
        fail_start.assert_called_once()
        popen.assert_not_called()

    def test_multiple_notes_use_one_crawler_process_and_finish_individually(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            browser_path = self._make_browser_executable(temp_dir)
            executor = CommentTaskExecutor(
                os.path.join(temp_dir, "comments.db"),
                max_comments=30,
                browser_path=browser_path,
            )
            posts = [{"note_id": "note-1"}, {"note_id": "note-2"}]
            post_data = {
                note_id: {
                    "note_id": note_id,
                    "note_url": f"https://www.xiaohongshu.com/explore/{note_id}",
                    "xsec_token": f"token-{note_id}",
                }
                for note_id in ("note-1", "note-2")
            }
            counts = {"note-1": 3, "note-2": 4}
            output = io.StringIO(
                "[browser-launch] isolated chromium\n"
                "[specified-note-start] note_id=note-1\n"
                "[specified-note-complete] note_id=note-1\n"
                "[specified-note-start] note_id=note-2\n"
                "[specified-note-complete] note_id=note-2\n"
            )
            fake_process = mock.Mock(stdout=output)
            fake_process.wait.return_value = 0

            with (
                mock.patch.object(executor, "get_post", side_effect=lambda note_id: post_data[note_id]),
                mock.patch.object(executor, "saved_comment_count", side_effect=lambda note_id: counts[note_id]),
                mock.patch.object(comment_fetcher.subprocess, "Popen", return_value=fake_process) as popen,
                mock.patch.object(comment_fetcher, "update_post_status") as update_status,
            ):
                try:
                    ok = executor.run_batch("task-one-browser", posts, io.StringIO())
                finally:
                    executor.close()

        self.assertTrue(ok)
        popen.assert_called_once()
        command = popen.call_args.args[0]
        specified_urls = command[command.index("--specified_id") + 1]
        self.assertIn("note-1", specified_urls)
        self.assertIn("note-2", specified_urls)
        self.assertEqual(command[command.index("--max_concurrency_num") + 1], "1")
        self.assertEqual(command[command.index("--headless") + 1], "yes")
        completed_ids = [
            call.args[1]
            for call in update_status.call_args_list
            if call.args[2] == "completed"
        ]
        self.assertEqual(completed_ids, ["note-1", "note-2"])

    def test_parent_task_stops_remaining_batches_after_risk_control(self):
        posts = [
            {"note_id": f"note-{index}", "comment_count_before": 0}
            for index in range(3)
        ]
        observed = {"batches": 0, "updates": [], "finished": 0}

        class FakeExecutor:
            def __init__(self, *args, **kwargs):
                self.last_exit_code = 0

            def run_batch(self, task_id, batch, log_file):
                observed["batches"] += 1
                self.last_exit_code = RISK_CONTROL_EXIT_CODE
                return False

            def saved_comment_count(self, note_id):
                return 0

            def close(self):
                return None

        def fake_update(*args):
            observed["updates"].append(args)

        with tempfile.TemporaryDirectory() as temp_dir:
            browser_path = self._make_browser_executable(temp_dir)
            with (
                mock.patch.object(
                    comment_fetcher,
                    "get_task",
                    return_value={
                        "status": "starting",
                        "config_json": json.dumps({
                            "batch_size": 1,
                            "max_comments": 30,
                            "browser_path": browser_path,
                        }),
                    },
                ),
                mock.patch.object(comment_fetcher, "get_task_posts", return_value=posts),
                mock.patch.object(comment_fetcher, "start_task"),
                mock.patch.object(
                    comment_fetcher,
                    "finish_task",
                    side_effect=lambda *_: observed.__setitem__("finished", observed["finished"] + 1),
                ),
                mock.patch.object(comment_fetcher, "update_post_status", side_effect=fake_update),
                mock.patch.object(comment_fetcher, "record_completion"),
                mock.patch.object(comment_fetcher, "CommentTaskExecutor", FakeExecutor),
                mock.patch.object(comment_fetcher, "DASHBOARD_DIR", Path(temp_dir)),
                mock.patch.object(
                    os.sys,
                    "argv",
                    [
                        "comment_fetcher.py", "--task-id", "task-risk",
                        "--browser-path", browser_path,
                    ],
                ),
            ):
                exit_code = comment_fetcher.main()

        self.assertEqual(exit_code, RISK_CONTROL_EXIT_CODE)
        self.assertEqual(observed["batches"], 1)
        self.assertEqual(observed["finished"], 1)
        self.assertEqual(len(observed["updates"]), len(posts))
        self.assertTrue(all(call[2] == "failed" for call in observed["updates"]))


if __name__ == "__main__":
    unittest.main()
