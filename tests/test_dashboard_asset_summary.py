import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dashboard"))

import server


SCHEMA = """
CREATE TABLE xhs_note (
  note_id TEXT PRIMARY KEY, title TEXT, desc TEXT, source_keyword TEXT,
  liked_count INTEGER, collected_count INTEGER, comment_count INTEGER,
  add_ts INTEGER, time INTEGER
);
CREATE TABLE xhs_note_comment (comment_id TEXT PRIMARY KEY, note_id TEXT);
CREATE INDEX idx_comment_note ON xhs_note_comment(note_id);
CREATE TABLE xhs_note_keyword_hit (
  note_id TEXT, keyword TEXT, task_id TEXT DEFAULT '',
  UNIQUE(note_id, keyword, task_id)
);
CREATE INDEX idx_hit_keyword ON xhs_note_keyword_hit(keyword);
"""


class AssetSummaryApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "crawler.db"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT INTO xhs_note VALUES (?,?,?,?,?,?,?,?,?)",
            [
                ("n1", "同一帖子", "有内容", "词A", 20, 2, 12, 3000, 1000),
                ("n2", "评论缺口", "有内容", "词A", 30, 3, 20, 4000, 2000),
                ("n3", "无关帖子", "有内容", "词C", 99, 9, 99, 5000, 3000),
            ],
        )
        conn.executemany(
            "INSERT INTO xhs_note_comment VALUES (?,?)",
            [("c1", "n1"), ("c2", "n1")],
        )
        conn.execute(
            "INSERT INTO xhs_note_keyword_hit(note_id, keyword, task_id) VALUES (?,?,?)",
            ("n1", "词B", "task-1"),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_summary_deduplicates_multi_keyword_hits_and_excludes_other_scope(self):
        with (
            mock.patch.object(server, "_crawler_db_path", str(self.db_path)),
            mock.patch.object(server, "get_config_values", return_value=(["词A", "词B"], 100)),
        ):
            response = server.app.test_client().get("/api/asset-summary")

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["total_posts"], 2)
        self.assertEqual(data["posts_with_comments"], 1)
        self.assertEqual(data["total_comments"], 2)
        self.assertEqual(data["multi_keyword_posts"], 1)
        self.assertEqual(data["comment_gap_candidates"], 2)
        coverage = {row["keyword"]: row for row in data["keyword_coverage"]}
        self.assertEqual(coverage["词A"]["post_count"], 2)
        self.assertEqual(coverage["词B"]["post_count"], 1)

    def test_legacy_database_without_keyword_hit_table_uses_source_keyword(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("DROP TABLE xhs_note_keyword_hit")
        conn.commit()
        conn.close()
        with (
            mock.patch.object(server, "_crawler_db_path", str(self.db_path)),
            mock.patch.object(server, "get_config_values", return_value=(["词A"], 100)),
        ):
            response = server.app.test_client().get("/api/asset-summary")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["total_posts"], 2)


if __name__ == "__main__":
    unittest.main()
