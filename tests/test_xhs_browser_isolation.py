"""XHS browser launch safety tests; no browser process is started."""

from unittest import mock

import pytest

import config
from media_platform.xhs.core import XiaoHongShuCrawler


class FakeChromium:
    def __init__(self):
        self.launch_kwargs = None

    async def launch(self, **kwargs):
        self.launch_kwargs = kwargs
        return mock.AsyncMock()


@pytest.mark.asyncio
async def test_xhs_does_not_fall_back_to_system_chrome(monkeypatch):
    monkeypatch.setattr(config, "CUSTOM_BROWSER_PATH", "")
    monkeypatch.setattr(config, "SAVE_LOGIN_STATE", False)
    chromium = FakeChromium()

    await XiaoHongShuCrawler.launch_browser(
        object(), chromium, None, "test-agent", headless=True
    )

    assert "executable_path" not in chromium.launch_kwargs


@pytest.mark.asyncio
async def test_xhs_rejects_explicit_system_chrome(monkeypatch):
    monkeypatch.setattr(
        config,
        "CUSTOM_BROWSER_PATH",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    )
    monkeypatch.setattr(config, "SAVE_LOGIN_STATE", False)

    with pytest.raises(RuntimeError, match="system Google Chrome is forbidden"):
        await XiaoHongShuCrawler.launch_browser(
            object(), FakeChromium(), None, "test-agent", headless=True
        )
