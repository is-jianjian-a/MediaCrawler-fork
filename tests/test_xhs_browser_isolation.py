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

    async def launch_persistent_context(self, **kwargs):
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


@pytest.mark.asyncio
async def test_xhs_profile_exits_when_last_window_is_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CUSTOM_BROWSER_PATH", "")
    monkeypatch.setattr(config, "SAVE_LOGIN_STATE", True)
    monkeypatch.setattr(config, "PLATFORM", "xhs")
    monkeypatch.setattr(config, "USER_DATA_DIR", "%s_user_data_dir_test")
    monkeypatch.chdir(tmp_path)
    chromium = FakeChromium()

    await XiaoHongShuCrawler.launch_browser(
        object(), chromium, None, "test-agent", headless=False
    )

    assert chromium.launch_kwargs["args"] == ["--disable-background-mode"]
    assert chromium.launch_kwargs["user_data_dir"] == str(
        config.BROWSER_DATA_ROOT / "xhs_user_data_dir_test"
    )
