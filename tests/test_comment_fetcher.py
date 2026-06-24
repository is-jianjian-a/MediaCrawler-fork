import unittest

from dashboard.comment_fetcher import CommentTaskExecutor


class CommentFetcherTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
