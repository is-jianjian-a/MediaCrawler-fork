import os
import unittest
from unittest import mock

from dashboard import crawl_runner
from media_platform.xhs.core import XiaoHongShuCrawler


class CrawlRunnerCdpPortTests(unittest.TestCase):
    def test_dashboard_actual_cdp_port_overrides_saved_preference(self):
        task = {
            "id": "crawl-test",
            "keywords": ["手机看视频"],
            "config": {
                "cdp_debug_port": 9222,
                "enable_cdp": True,
                "stop_condition": "new_count",
                "max_count": 1,
            },
        }
        with mock.patch.dict(
            os.environ,
            {"MEDIACRAWLER_TASK_CDP_DEBUG_PORT": "9231"},
            clear=False,
        ):
            _, env = crawl_runner._build_command(task)

        self.assertEqual(env["MEDIACRAWLER_CDP_DEBUG_PORT"], "9231")


class ExistingPageReuseTests(unittest.IsolatedAsyncioTestCase):
    async def test_cdp_reuses_existing_xhs_page(self):
        crawler = XiaoHongShuCrawler()
        crawler.cdp_manager = object()
        other_page = mock.Mock(url="https://example.com")
        xhs_page = mock.Mock(url="https://www.xiaohongshu.com/explore")
        crawler.browser_context = mock.Mock(pages=[other_page, xhs_page])

        selected = await crawler._get_or_create_context_page()

        self.assertIs(selected, xhs_page)
        crawler.browser_context.new_page.assert_not_called()

    async def test_standard_mode_keeps_isolated_new_page(self):
        crawler = XiaoHongShuCrawler()
        crawler.cdp_manager = None
        new_page = mock.Mock(url="about:blank")
        crawler.browser_context = mock.Mock()
        crawler.browser_context.new_page = mock.AsyncMock(return_value=new_page)

        selected = await crawler._get_or_create_context_page()

        self.assertIs(selected, new_page)
        crawler.browser_context.new_page.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
