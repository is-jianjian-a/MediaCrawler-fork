# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/tools/cdp_browser.py
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1
#

# 声明：本代码仅供学习和研究目的使用。使用者应遵守以下原则：
# 1. 不得用于任何商业用途。
# 2. 使用时应遵守目标平台的使用条款和robots.txt规则。
# 3. 不得进行大规模爬取或对平台造成运营干扰。
# 4. 应合理控制请求频率，避免给目标平台带来不必要的负担。
# 5. 不得用于任何非法或不当的用途。
#
# 详细许可条款请参阅项目根目录下的LICENSE文件。
# 使用本代码即表示您同意遵守上述原则和LICENSE中的所有条款。


import os
import asyncio
import socket
import httpx
import signal
import atexit
from pathlib import Path
from typing import Optional, Dict, Any
from playwright.async_api import Browser, BrowserContext, Playwright

import config
from tools.browser_launcher import BrowserLauncher
from tools import utils
from tools.crawler_util import get_random_viewport
import logging
logger = logging.getLogger("MediaCrawler")


class CDPBrowserManager:
    """
    CDP browser manager, responsible for launching and managing browsers connected via CDP
    """

    def __init__(self):
        self.launcher = BrowserLauncher()
        self.browser: Optional[Browser] = None
        self.browser_context: Optional[BrowserContext] = None
        self.debug_port: Optional[int] = None
        self._cleanup_registered = False

    def _register_cleanup_handlers(self):
        """
        Register cleanup handlers to ensure browser process cleanup on program exit
        """
        if self._cleanup_registered:
            return

        def sync_cleanup():
            """Synchronous cleanup function for atexit"""
            if not config.AUTO_CLOSE_BROWSER:
                utils.logger.info("[CDPBrowserManager] atexit: Browser process kept running (AUTO_CLOSE_BROWSER=false)")
                return
            if self.launcher and self.launcher.browser_process:
                utils.logger.info("[CDPBrowserManager] atexit: Cleaning up browser process")
                self.launcher.cleanup()

        # Register atexit cleanup
        atexit.register(sync_cleanup)

        # Register signal handlers (only when no custom handlers exist, to avoid overriding main entry signal handling logic)
        prev_sigint = signal.getsignal(signal.SIGINT)
        prev_sigterm = signal.getsignal(signal.SIGTERM)

        def signal_handler(signum, frame):
            """Signal handler"""
            utils.logger.info(f"[CDPBrowserManager] Received signal {signum}")
            if self.launcher and self.launcher.browser_process and config.AUTO_CLOSE_BROWSER:
                utils.logger.info("[CDPBrowserManager] Cleaning up browser process")
                self.launcher.cleanup()
            elif self.launcher and self.launcher.browser_process:
                utils.logger.info("[CDPBrowserManager] Browser process kept running (AUTO_CLOSE_BROWSER=false)")

            if signum == signal.SIGINT:
                if prev_sigint == signal.default_int_handler:
                    return prev_sigint(signum, frame)
                raise KeyboardInterrupt

            raise SystemExit(0)

        install_sigint = prev_sigint in (signal.default_int_handler, signal.SIG_DFL)
        install_sigterm = prev_sigterm == signal.SIG_DFL

        # Register SIGINT (Ctrl+C) and SIGTERM
        if install_sigint:
            signal.signal(signal.SIGINT, signal_handler)
        else:
            utils.logger.info("[CDPBrowserManager] SIGINT handler already exists, skipping registration to avoid override")

        if install_sigterm:
            signal.signal(signal.SIGTERM, signal_handler)
        else:
            utils.logger.info("[CDPBrowserManager] SIGTERM handler already exists, skipping registration to avoid override")

        self._cleanup_registered = True
        utils.logger.info("[CDPBrowserManager] Cleanup handlers registered")

    async def launch_and_connect(
        self,
        playwright: Playwright,
        playwright_proxy: Optional[Dict] = None,
        user_agent: Optional[str] = None,
        headless: bool = False,
    ) -> BrowserContext:
        """
        Launch browser and connect via CDP
        """
        try:
            if config.CDP_CONNECT_EXISTING:
                # Connect to an existing browser that already has remote debugging enabled
                return await self._connect_existing_browser(playwright, playwright_proxy, user_agent)

            # 1. Detect browser path
            browser_path = await self._get_browser_path()

            # 2. Get available port
            self.debug_port = self.launcher.find_available_port(config.CDP_DEBUG_PORT)

            # 3. Launch browser
            await self._launch_browser(browser_path, headless)

            # 4. Register cleanup handlers (ensure cleanup on abnormal exit)
            self._register_cleanup_handlers()

            # 5. Connect via CDP
            await self._connect_via_cdp(playwright)

            # 6. Create browser context
            browser_context = await self._create_browser_context(
                playwright_proxy, user_agent
            )

            self.browser_context = browser_context
            return browser_context

        except Exception as e:
            utils.logger.error(f"[CDPBrowserManager] CDP browser launch failed: {e}")
            await self.cleanup()
            raise

    async def _connect_existing_browser(
        self,
        playwright: Playwright,
        playwright_proxy: Optional[Dict] = None,
        user_agent: Optional[str] = None,
    ) -> BrowserContext:
        """
        Connect to an existing browser that already has remote debugging enabled.
        User needs to enable remote debugging via chrome://inspect/#remote-debugging
        or launch Chrome with --remote-debugging-port flag.
        """
        self.debug_port = config.CDP_DEBUG_PORT
        utils.logger.info(
            f"[CDPBrowserManager] Connecting to existing browser on port {self.debug_port}..."
        )
        utils.logger.info(
            "[CDPBrowserManager] Make sure remote debugging is enabled in your browser: "
            "chrome://inspect/#remote-debugging"
        )

        # Wait for the browser's CDP port to become available
        # The user may need time to enable remote debugging or confirm the connection dialog
        timeout = config.BROWSER_LAUNCH_TIMEOUT
        utils.logger.info(
            f"[CDPBrowserManager] Waiting up to {timeout}s for browser CDP connection..."
        )

        # If the port is already open but /json/version is not a valid DevTools
        # endpoint, it is usually a normal Chrome instance or Chrome's
        # non-Playwright-compatible remote-debugging toggle occupying the port.
        # Waiting 60s will not fix that; fail fast and let the crawler fallback
        # to standard browser mode unless REQUIRE_CDP_MODE is enabled by caller.
        if self._is_port_open(self.debug_port):
            if not await self._test_cdp_connection(self.debug_port):
                raise RuntimeError(
                    f"Port {self.debug_port} is open, but it is not a valid Chrome DevTools "
                    "CDP endpoint (/json/version is unavailable)."
                )

        connected = False
        for i in range(timeout):
            if await self._test_cdp_connection(self.debug_port):
                connected = True
                break
            if i % 5 == 0 and i > 0:
                utils.logger.info(
                    f"[CDPBrowserManager] Still waiting for browser... ({i}s elapsed) "
                    "Please enable remote debugging: chrome://inspect/#remote-debugging"
                )
            await asyncio.sleep(1)

        if not connected:
            raise RuntimeError(
                f"Cannot connect to existing browser on port {self.debug_port} "
                f"after waiting {timeout}s. Please ensure:\n"
                "  1. Your browser is running\n"
                "  2. Remote debugging is enabled (chrome://inspect/#remote-debugging)\n"
                f"  3. The debug port is {self.debug_port} (configure via CDP_DEBUG_PORT)"
            )

        # Connect via CDP (reuse existing method)
        await self._connect_via_cdp(playwright)

        # Create browser context (reuse existing method, will prefer existing context)
        browser_context = await self._create_browser_context(playwright_proxy, user_agent)
        self.browser_context = browser_context

        utils.logger.info("[CDPBrowserManager] Successfully connected to existing browser")
        return browser_context

    @staticmethod
    def _is_port_open(debug_port: int) -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.5)
                return sock.connect_ex(("127.0.0.1", debug_port)) == 0
        except Exception:
            logger.exception(f"Unhandled exception in _is_port_open()")
            return False

    async def _get_browser_path(self) -> str:
        """
        Get browser path
        """
        # Prefer user-defined path
        if config.CUSTOM_BROWSER_PATH and os.path.isfile(config.CUSTOM_BROWSER_PATH):
            utils.logger.info(
                f"[CDPBrowserManager] Using custom browser path: {config.CUSTOM_BROWSER_PATH}"
            )
            return config.CUSTOM_BROWSER_PATH

        # Auto-detect browser path
        browser_paths = self.launcher.detect_browser_paths()

        if not browser_paths:
            raise RuntimeError(
                "No available browser found. Please ensure Chrome or Edge browser is installed, "
                "or set CUSTOM_BROWSER_PATH in config file to specify browser path."
            )

        browser_path = browser_paths[0]  # Use the first browser found
        browser_name, browser_version = self.launcher.get_browser_info(browser_path)

        utils.logger.info(
            f"[CDPBrowserManager] Detected browser: {browser_name} ({browser_version})"
        )
        utils.logger.info(f"[CDPBrowserManager] Browser path: {browser_path}")

        return browser_path

    async def _test_cdp_connection(self, debug_port: int) -> bool:
        """
        Test if CDP connection is available
        """
        try:
            ws_url = await self._get_browser_websocket_url(debug_port)
            utils.logger.info(
                f"[CDPBrowserManager] CDP endpoint {debug_port} is ready: {ws_url}"
            )
            return True
        except Exception as e:
            utils.logger.warning(f"[CDPBrowserManager] CDP endpoint test failed: {e}")
            return False

    async def _launch_browser(self, browser_path: str, headless: bool):
        """
        Launch browser process
        """
        # Set user data directory (if save login state is enabled)
        user_data_dir = None
        if config.SAVE_LOGIN_STATE:
            user_data_dir = os.path.join(
                os.getcwd(),
                "browser_data",
                f"cdp_{config.USER_DATA_DIR % config.PLATFORM}",
            )
            os.makedirs(user_data_dir, exist_ok=True)
            utils.logger.info(f"[CDPBrowserManager] User data directory: {user_data_dir}")

        # Launch browser
        self.launcher.browser_process = self.launcher.launch_browser(
            browser_path=browser_path,
            debug_port=self.debug_port,
            headless=headless,
            user_data_dir=user_data_dir,
        )

        # Wait for browser to be ready
        if not self.launcher.wait_for_browser_ready(
            self.debug_port, config.BROWSER_LAUNCH_TIMEOUT
        ):
            raise RuntimeError(f"Browser failed to start within {config.BROWSER_LAUNCH_TIMEOUT} seconds")

        # Extra wait for CDP service to fully start
        await asyncio.sleep(1)

        # Test CDP connection
        if not await self._test_cdp_connection(self.debug_port):
            utils.logger.warning(
                "[CDPBrowserManager] CDP connection test failed, but will continue to try connecting"
            )

    async def _get_browser_websocket_url(self, debug_port: int) -> str:
        """
        Get browser WebSocket connection URL
        """
        last_error = None
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"http://127.0.0.1:{debug_port}/json/version", timeout=10
                )
                if response.status_code == 200:
                    data = response.json()
                    ws_url = data.get("webSocketDebuggerUrl")
                    if ws_url:
                        utils.logger.info(
                            f"[CDPBrowserManager] Got browser WebSocket URL: {ws_url}"
                        )
                        return ws_url
                    else:
                        raise RuntimeError("webSocketDebuggerUrl not found")
                else:
                    raise RuntimeError(f"HTTP {response.status_code}: {response.text}")
        except Exception as e:
            last_error = e
            utils.logger.warning(
                f"[CDPBrowserManager] Failed to get WebSocket URL from /json/version: {e}"
            )

        ws_url = self._get_browser_websocket_url_from_active_port(debug_port)
        if ws_url:
            utils.logger.info(
                f"[CDPBrowserManager] Got browser WebSocket URL from DevToolsActivePort: {ws_url}"
            )
            return ws_url

        utils.logger.error(f"[CDPBrowserManager] Failed to get WebSocket URL: {last_error}")
        raise last_error or RuntimeError("Failed to get browser WebSocket URL")

    def _get_browser_websocket_url_from_active_port(self, debug_port: int) -> Optional[str]:
        """Read Chrome's DevToolsActivePort file.

        Newer Chrome's chrome://inspect/#remote-debugging toggle may open the
        browser-level WebSocket and write DevToolsActivePort, but not expose the
        traditional /json/version HTTP discovery endpoint.
        """
        candidates = []
        if config.CUSTOM_BROWSER_PATH:
            candidates.append(Path(config.CUSTOM_BROWSER_PATH).parent / "DevToolsActivePort")
        candidates.extend([
            Path.home() / "Library/Application Support/Google/Chrome/DevToolsActivePort",
            Path.home() / "Library/Application Support/Google/Chrome/Default/DevToolsActivePort",
            Path(os.getcwd()) / "browser_data" / (config.USER_DATA_DIR % config.PLATFORM) / "DevToolsActivePort",
            Path(os.getcwd()) / "browser_data" / f"cdp_{config.USER_DATA_DIR % config.PLATFORM}" / "DevToolsActivePort",
        ])
        for candidate in candidates:
            try:
                if not candidate.exists():
                    continue
                lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
                if len(lines) < 2:
                    continue
                port = int(lines[0].strip())
                browser_path = lines[1].strip()
                if port != debug_port or not browser_path.startswith("/devtools/browser/"):
                    continue
                return f"ws://127.0.0.1:{port}{browser_path}"
            except Exception as exc:
                utils.logger.debug(
                    f"[CDPBrowserManager] Failed reading DevToolsActivePort {candidate}: {exc}"
                )
        return None

    async def _connect_via_cdp(self, playwright: Playwright):
        """
        Connect to browser via CDP
        """
        try:
            ws_url = await self._get_browser_websocket_url(self.debug_port)
            if config.CDP_CONNECT_EXISTING:
                # For an existing browser, Chrome returns the concrete browser WebSocket
                # endpoint from /json/version. Do not hand-build /devtools/browser:
                # Chrome's remote-debugging toggle exposes a tokenized URL and the
                # non-tokenized path returns 404.
                utils.logger.info(f"[CDPBrowserManager] Connecting to existing browser via CDP: {ws_url}")
                utils.logger.info(
                    "[CDPBrowserManager] Please check your browser for a confirmation dialog and accept it"
                )
                self.browser = await playwright.chromium.connect_over_cdp(
                    ws_url, timeout=config.BROWSER_LAUNCH_TIMEOUT * 1000
                )
            else:
                utils.logger.info(f"[CDPBrowserManager] Connecting to browser via CDP: {ws_url}")
                self.browser = await playwright.chromium.connect_over_cdp(ws_url)

            if self.browser.is_connected():
                utils.logger.info("[CDPBrowserManager] Successfully connected to browser")
                utils.logger.info(
                    f"[CDPBrowserManager] Browser contexts count: {len(self.browser.contexts)}"
                )
            else:
                raise RuntimeError("CDP connection failed")

        except Exception as e:
            utils.logger.error(f"[CDPBrowserManager] CDP connection failed: {e}")
            raise

    async def _create_browser_context(
        self, playwright_proxy: Optional[Dict] = None, user_agent: Optional[str] = None
    ) -> BrowserContext:
        if not self.browser:
            raise RuntimeError("Browser not connected")

        contexts = self.browser.contexts

        if contexts:
            browser_context = contexts[0]
            utils.logger.info("[CDPBrowserManager] Using existing browser context")
        else:
            viewport = get_random_viewport()
            context_options = {
                "viewport": viewport,
                "accept_downloads": True,
            }

            if user_agent:
                context_options["user_agent"] = user_agent
                utils.logger.info(f"[CDPBrowserManager] Setting user agent: {user_agent}")

            if playwright_proxy:
                utils.logger.warning(
                    "[CDPBrowserManager] Warning: Proxy settings may not work in CDP mode, "
                    "recommend configuring system proxy or browser proxy extension before launching browser"
                )

            browser_context = await self.browser.new_context(**context_options)
            utils.logger.info(f"[CDPBrowserManager] Created new browser context (viewport: {viewport['width']}x{viewport['height']})")

        await self.add_stealth_script()

        return browser_context

    async def add_stealth_script(self, script_path: str = "libs/stealth.min.js"):
        """
        Add anti-detection script
        """
        if self.browser_context and os.path.exists(script_path):
            try:
                await self.browser_context.add_init_script(path=script_path)
                utils.logger.info(
                    f"[CDPBrowserManager] Added anti-detection script: {script_path}"
                )
            except Exception as e:
                utils.logger.warning(f"[CDPBrowserManager] Failed to add anti-detection script: {e}")

    async def add_cookies(self, cookies: list):
        """
        Add cookies
        """
        if self.browser_context:
            try:
                await self.browser_context.add_cookies(cookies)
                utils.logger.info(f"[CDPBrowserManager] Added {len(cookies)} cookies")
            except Exception as e:
                utils.logger.warning(f"[CDPBrowserManager] Failed to add cookies: {e}")

    async def get_cookies(self) -> list:
        """
        Get current cookies
        """
        if self.browser_context:
            try:
                cookies = await self.browser_context.cookies()
                return cookies
            except Exception as e:
                utils.logger.warning(f"[CDPBrowserManager] Failed to get cookies: {e}")
                return []
        return []

    async def cleanup(self, force: bool = False):
        """
        Cleanup resources

        Args:
            force: Whether to force cleanup browser process (ignoring AUTO_CLOSE_BROWSER config)
        """
        try:
            if not force and not config.AUTO_CLOSE_BROWSER:
                utils.logger.info("[CDPBrowserManager] Cleanup skipped; browser kept running (AUTO_CLOSE_BROWSER=false)")
                self.browser_context = None
                self.browser = None
                return

            # Close browser context
            if self.browser_context:
                try:
                    if config.CDP_CONNECT_EXISTING:
                        utils.logger.info(
                            "[CDPBrowserManager] Connected to existing browser, keeping browser context open"
                        )
                    else:
                        # Check if context is already closed
                        # Try to get page list, if fails means already closed
                        try:
                            pages = self.browser_context.pages
                            if pages is not None:
                                await self.browser_context.close()
                                utils.logger.info("[CDPBrowserManager] Browser context closed")
                        except Exception:
                            utils.logger.debug("[CDPBrowserManager] Browser context already closed")
                except Exception as context_error:
                    # Only log warning if error is not due to already being closed
                    error_msg = str(context_error).lower()
                    if "closed" not in error_msg and "disconnected" not in error_msg:
                        utils.logger.warning(
                            f"[CDPBrowserManager] Failed to close browser context: {context_error}"
                        )
                    else:
                        utils.logger.debug(f"[CDPBrowserManager] Browser context already closed: {context_error}")
                finally:
                    self.browser_context = None

            # Disconnect browser
            if self.browser:
                try:
                    # Check if browser is still connected
                    if config.CDP_CONNECT_EXISTING:
                        utils.logger.info(
                            "[CDPBrowserManager] Connected to existing browser, keeping browser connection open"
                        )
                    elif self.browser.is_connected():
                        await self.browser.close()
                        utils.logger.info("[CDPBrowserManager] Browser connection disconnected")
                    else:
                        utils.logger.debug("[CDPBrowserManager] Browser connection already disconnected")
                except Exception as browser_error:
                    # Only log warning if error is not due to already being closed
                    error_msg = str(browser_error).lower()
                    if "closed" not in error_msg and "disconnected" not in error_msg:
                        utils.logger.warning(
                            f"[CDPBrowserManager] Failed to close browser connection: {browser_error}"
                        )
                    else:
                        utils.logger.debug(f"[CDPBrowserManager] Browser connection already closed: {browser_error}")
                finally:
                    self.browser = None

            # Close browser process (skip if connected to existing browser - we didn't launch it)
            if config.CDP_CONNECT_EXISTING:
                utils.logger.info(
                    "[CDPBrowserManager] Connected to existing browser, skipping process cleanup"
                )
            elif force or config.AUTO_CLOSE_BROWSER:
                # force=True means force close, ignoring AUTO_CLOSE_BROWSER config
                # Used for handling abnormal exit or manual cleanup
                if self.launcher and self.launcher.browser_process:
                    self.launcher.cleanup()
                else:
                    utils.logger.debug("[CDPBrowserManager] No browser process to cleanup")
            else:
                utils.logger.info(
                    "[CDPBrowserManager] Browser process kept running (AUTO_CLOSE_BROWSER=False)"
                )

        except Exception as e:
            utils.logger.error(f"[CDPBrowserManager] Error during resource cleanup: {e}")

    def is_connected(self) -> bool:
        """
        Check if connected to browser
        """
        return self.browser is not None and self.browser.is_connected()

    async def get_browser_info(self) -> Dict[str, Any]:
        """
        Get browser info
        """
        if not self.browser:
            return {}

        try:
            version = self.browser.version
            contexts_count = len(self.browser.contexts)

            return {
                "version": version,
                "contexts_count": contexts_count,
                "debug_port": self.debug_port,
                "is_connected": self.is_connected(),
            }
        except Exception as e:
            utils.logger.warning(f"[CDPBrowserManager] Failed to get browser info: {e}")
            return {}
