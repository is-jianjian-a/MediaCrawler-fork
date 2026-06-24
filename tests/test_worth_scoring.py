import unittest

from dashboard.worth_scoring import score_post


class WorthScoringTests(unittest.TestCase):
    def test_comment_gap_and_coverage_drive_score(self):
        sparse = score_post({
            "liked_count": 2000, "comment_count": 1000,
            "db_comment_count": 5, "desc_length": 300, "title": "用户问题",
        })
        covered = score_post({
            "liked_count": 2000, "comment_count": 1000,
            "db_comment_count": 900, "desc_length": 300, "title": "用户问题",
        })

        self.assertEqual(sparse["comment_gap"], 995)
        self.assertEqual(sparse["coverage_rate"], 0.005)
        self.assertGreater(sparse["worth_score"], covered["worth_score"])

    def test_saved_comments_never_create_negative_gap(self):
        result = score_post({"comment_count": 10, "db_comment_count": 12})
        self.assertEqual(result["comment_gap"], 0)
        self.assertEqual(result["coverage_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
