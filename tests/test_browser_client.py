import os
from unittest.mock import patch, MagicMock

import httpx
import pytest
from patchright.sync_api import TimeoutError as PatchrightTimeoutError

from boss_agent_cli.api import endpoints
from boss_agent_cli.api.browser_client import (
	CDP_DEFAULT_URL,
	HOME_URL,
	BrowserPageNotReady,
	BrowserStageError,
	_FETCH_TIMEOUT_MS,
	_NAV_TIMEOUT_MS,
	_SAFE_DOMCONTENTLOADED_GRACE_MS,
	BrowserSession,
	_find_reusable_zhipin_page,
	_is_zhipin_url,
)


def test_browser_session_defaults():
	session = BrowserSession(cookies={"wt2": "abc"}, user_agent="test-ua")
	assert session._is_cdp is False
	assert session._started is False
	assert session._cookies == {"wt2": "abc"}


def test_fetch_ws_url_success():
	with patch("httpx.get") as mock_get:
		mock_resp = MagicMock()
		mock_resp.json.return_value = {"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/abc"}
		mock_get.return_value = mock_resp
		ws = BrowserSession._fetch_ws_url("http://127.0.0.1:9222")
		assert ws == "ws://127.0.0.1:9222/devtools/browser/abc"


def test_fetch_ws_url_failure():
	with patch("httpx.get", side_effect=httpx.ConnectError("connection refused")):
		ws = BrowserSession._fetch_ws_url("http://127.0.0.1:9222")
		assert ws is None


def test_fetch_ws_url_invalid_json_returns_none():
	with patch("httpx.get") as mock_get:
		mock_resp = MagicMock()
		mock_resp.json.side_effect = ValueError("invalid json")
		mock_get.return_value = mock_resp

		ws = BrowserSession._fetch_ws_url("http://127.0.0.1:9222")

		assert ws is None


def test_fetch_ws_url_missing_websocket_debugger_url_returns_none():
	with patch("httpx.get") as mock_get:
		mock_resp = MagicMock()
		mock_resp.json.return_value = {"Browser": "Chrome"}
		mock_get.return_value = mock_resp

		ws = BrowserSession._fetch_ws_url("http://127.0.0.1:9222")

		assert ws is None


def test_fetch_ws_url_non_object_json_returns_none():
	with patch("httpx.get") as mock_get:
		mock_resp = MagicMock()
		mock_resp.json.return_value = ["not", "a", "devtools", "object"]
		mock_get.return_value = mock_resp

		ws = BrowserSession._fetch_ws_url("http://127.0.0.1:9222")

		assert ws is None


def test_read_devtools_active_port_missing(tmp_path):
	with patch("boss_agent_cli.api.browser_client._CHROME_USER_DATA_CANDIDATES", [tmp_path / "nonexistent"]):
		ws = BrowserSession._read_devtools_active_port()
		assert ws is None


def test_read_devtools_active_port_found(tmp_path):
	port_file = tmp_path / "DevToolsActivePort"
	port_file.write_text("9222\n/devtools/browser/test-id\n")
	with patch("boss_agent_cli.api.browser_client._CHROME_USER_DATA_CANDIDATES", [tmp_path]):
		ws = BrowserSession._read_devtools_active_port()
		assert ws == "ws://127.0.0.1:9222/devtools/browser/test-id"


def test_close_cdp_mode_reused_context_not_closed():
	"""CDP 复用用户 context 时 close() 只关闭 page，不关闭 context"""
	session = BrowserSession(cookies={}, user_agent="")
	session._is_cdp = True
	session._own_context = False  # 复用的 context
	session._started = True
	session._page = MagicMock()
	session._context = MagicMock()
	session._browser = MagicMock()
	session._pw = MagicMock()

	session.close()

	session._page.close.assert_called_once()
	session._context.close.assert_not_called()  # 不关闭用户的 context
	session._browser.close.assert_not_called()


def test_close_cdp_mode_own_context_closed():
	"""CDP 自建 context 时 close() 关闭 page 和 context"""
	session = BrowserSession(cookies={}, user_agent="")
	session._is_cdp = True
	session._own_context = True  # 自建的 context
	session._started = True
	session._page = MagicMock()
	session._context = MagicMock()
	session._browser = MagicMock()
	session._pw = MagicMock()

	session.close()

	session._page.close.assert_called_once()
	session._context.close.assert_called_once()


def test_close_headless_mode_closes_browser():
	"""Headless 模式下 close() 关闭整个 browser"""
	session = BrowserSession(cookies={}, user_agent="")
	session._is_cdp = False
	session._started = True
	session._page = MagicMock()
	session._browser = MagicMock()
	session._pw = MagicMock()

	session.close()

	session._browser.close.assert_called_once()


def test_close_is_idempotent_when_cdp_resources_are_partial_and_raise():
	session = BrowserSession(cookies={}, user_agent="")
	session._is_cdp = True
	session._own_context = True
	session._started = True
	session._page = MagicMock()
	session._context = MagicMock()
	session._pw = MagicMock()
	session._page.close.side_effect = RuntimeError("page already closed")
	session._context.close.side_effect = RuntimeError("context already closed")
	session._pw.stop.side_effect = RuntimeError("playwright already stopped")

	session.close()
	session.close()

	assert session._started is False
	assert session._page.close.call_count == 2
	assert session._context.close.call_count == 2
	assert session._pw.stop.call_count == 2


def test_close_is_idempotent_when_headless_resources_are_partial_and_raise():
	session = BrowserSession(cookies={}, user_agent="")
	session._is_cdp = False
	session._started = True
	session._browser = MagicMock()
	session._pw = MagicMock()
	session._browser.close.side_effect = RuntimeError("browser already closed")
	session._pw.stop.side_effect = RuntimeError("playwright already stopped")

	session.close()
	session.close()

	assert session._started is False
	assert session._browser.close.call_count == 2
	assert session._pw.stop.call_count == 2


def test_try_connect_reuses_existing_context():
	"""CDP 连接应复用用户现有 context，避免创建额外浏览器状态。"""
	session = BrowserSession(cookies={}, user_agent="")
	session._pw = MagicMock()

	mock_browser = MagicMock()
	mock_user_context = MagicMock()
	mock_browser.contexts = [mock_user_context]
	mock_page = MagicMock()
	mock_user_context.new_page.return_value = mock_page

	session._pw.chromium.connect_over_cdp.return_value = mock_browser

	result = session._try_connect("ws://localhost:9222/test")

	assert result is True
	assert session._is_cdp is True
	assert session._own_context is False  # 复用，非自建
	assert session._context is mock_user_context  # 直接使用用户 context
	# 验证：没有创建新 context
	mock_browser.new_context.assert_not_called()
	# 验证：page 在用户 context 中创建
	mock_user_context.new_page.assert_called_once()


def test_try_connect_creates_new_context_when_none_exists():
	"""CDP 连接无已存在 context 时创建新 context 并注入 cookies"""
	session = BrowserSession(cookies={"wt2": "abc"}, user_agent="")
	session._pw = MagicMock()

	mock_browser = MagicMock()
	mock_browser.contexts = []  # 无已存在 context
	mock_new_context = MagicMock()
	mock_browser.new_context.return_value = mock_new_context
	mock_page = MagicMock()
	mock_new_context.new_page.return_value = mock_page

	session._pw.chromium.connect_over_cdp.return_value = mock_browser

	result = session._try_connect("ws://localhost:9222/test")

	assert result is True
	assert session._is_cdp is True
	assert session._own_context is True  # 自建
	# 验证：创建了新 context
	mock_browser.new_context.assert_called_once()
	# 验证：cookies 被注入
	mock_new_context.add_cookies.assert_called_once()
	cookies_arg = mock_new_context.add_cookies.call_args[0][0]
	assert any(c["name"] == "wt2" for c in cookies_arg)


def test_start_headless_tolerates_networkidle_timeout():
	"""Headless 预热不应因 networkidle 等待超时而直接失败。"""
	logger = MagicMock()
	session = BrowserSession(cookies={"wt2": "abc"}, user_agent="UA", logger=logger)
	session._pw = MagicMock()

	mock_browser = MagicMock()
	mock_context = MagicMock()
	mock_page = MagicMock()
	def wait_for_load_state(state, **kwargs):
		if state == "networkidle":
			raise Exception("Timeout 30000ms exceeded")
	mock_page.wait_for_load_state.side_effect = wait_for_load_state
	mock_context.new_page.return_value = mock_page
	mock_browser.new_context.return_value = mock_context
	session._pw.chromium.launch.return_value = mock_browser

	session._start_headless()

	assert session._started is True
	assert session._is_cdp is False
	mock_page.goto.assert_called_once_with(
		HOME_URL,
		wait_until="domcontentloaded",
		timeout=_NAV_TIMEOUT_MS,
	)
	assert [call.args[0] for call in mock_page.wait_for_load_state.call_args_list] == [
		"domcontentloaded", "networkidle"
	]
	logger.debug.assert_any_call(
		"[boss] CDP 不可用（提示：需以 --remote-debugging-port=9222 启动 Chrome），降级到 headless patchright"
	)
	assert any("headless 首页未进入 networkidle" in call.args[0] for call in logger.debug.call_args_list)


def test_start_headless_rejects_navigation_timeout_before_fetch():
	"""导航未完成时返回稳定错误，绝不继续执行 fetch。"""
	session = BrowserSession(cookies={"wt2": "abc"}, user_agent="UA")
	session._pw = MagicMock()

	mock_browser = MagicMock()
	mock_context = MagicMock()
	mock_page = MagicMock()
	mock_page.url = "https://www.zhipin.com/"
	mock_page.goto.side_effect = RuntimeError("navigation timeout")
	mock_context.new_page.return_value = mock_page
	mock_browser.new_context.return_value = mock_context
	session._pw.chromium.launch.return_value = mock_browser

	with pytest.raises(BrowserPageNotReady) as error:
		session._start_headless()

	assert error.value.stage == "navigate_domcontentloaded"
	assert session._started is False
	mock_page.wait_for_load_state.assert_not_called()


def test_start_headless_reports_context_launch_stage():
	"""浏览器 context 启动失败只暴露固定阶段，不携带底层异常正文。"""
	session = BrowserSession(cookies={"wt2": "abc"}, user_agent="UA")
	session._pw = MagicMock()
	session._pw.chromium.launch.side_effect = RuntimeError("private launch details")

	with pytest.raises(RuntimeError) as error:
		session._start_headless()

	assert str(error.value) == "BROWSER_ERROR"
	assert getattr(error.value, "stage") == "context_launch"


def test_safe_browser_uses_persistent_profile_with_fresh_visible_page(tmp_path):
	"""安全入口复用扫码 profile，但不复用可能卡在导航中的恢复页。"""
	with patch.dict(
		os.environ,
		{
			"BOSS_SAFE_ISOLATED_BROWSER": "1",
			"BOSS_SAFE_BROWSER_PROFILE": str(tmp_path / "browser-profile"),
		},
	):
		session = BrowserSession(cookies={"wt2": "abc"}, user_agent="UA")
	session._pw = MagicMock()

	mock_context = MagicMock()
	stale_page = MagicMock()
	stale_page.url = HOME_URL
	stale_page.wait_for_load_state.side_effect = RuntimeError("stale navigation")
	mock_context.pages = [stale_page]
	mock_page = MagicMock()
	mock_page.url = HOME_URL
	mock_context.new_page.return_value = mock_page
	session._pw.chromium.launch_persistent_context.return_value = mock_context

	session._start_headless()

	profile_arg = session._pw.chromium.launch_persistent_context.call_args.args[0]
	assert profile_arg == str(tmp_path / "browser-profile")
	assert session._pw.chromium.launch_persistent_context.call_args.kwargs["headless"] is False
	session._pw.chromium.launch.assert_not_called()
	assert session._is_persistent is True
	assert session._page is mock_page
	stale_page.wait_for_load_state.assert_not_called()
	mock_page.goto.assert_called_once_with(
		HOME_URL,
		wait_until="commit",
		timeout=_NAV_TIMEOUT_MS,
	)
	assert mock_page.wait_for_load_state.call_args_list[0].args == ("domcontentloaded",)
	assert mock_page.wait_for_load_state.call_args_list[0].kwargs == {
		"timeout": _SAFE_DOMCONTENTLOADED_GRACE_MS,
	}
	assert session.readiness_stage == "navigate_domcontentloaded"
	mock_context.add_cookies.assert_called_once()

	session.close()
	mock_context.close.assert_called_once()


def test_safe_browser_allows_commit_only_after_domcontentloaded_timeout(tmp_path):
	"""commit 成功后安全入口通过共享 Cookie 的 context 请求，不再 evaluate。"""
	with patch.dict(
		os.environ,
		{
			"BOSS_SAFE_ISOLATED_BROWSER": "1",
			"BOSS_SAFE_BROWSER_PROFILE": str(tmp_path / "browser-profile"),
		},
	):
		session = BrowserSession(cookies={"wt2": "abc"}, user_agent="UA")
	session._pw = MagicMock()
	mock_context = MagicMock()
	mock_page = MagicMock()
	mock_page.url = HOME_URL
	mock_page.wait_for_load_state.side_effect = RuntimeError("dom still loading")
	mock_response = MagicMock()
	mock_response.json.return_value = {"code": 0, "zpData": {"jobList": []}}
	mock_context.request.fetch.return_value = mock_response
	mock_context.new_page.return_value = mock_page
	session._pw.chromium.launch_persistent_context.return_value = mock_context

	session._start_headless()
	url = endpoints.SEARCH_URL
	result = session.request("GET", url, params={"query": "AI Agent", "page": 1, "unused": None})

	assert session.readiness_stage == "navigate_commit_only"
	assert session._commit_ready is True
	assert result["code"] == 0
	mock_context.request.fetch.assert_called_once_with(
		url,
		method="GET",
		params={"query": "AI Agent", "page": "1"},
		headers={
			"Accept": "application/json, text/plain, */*",
			"Referer": endpoints.REFERER_MAP[url],
			"X-Requested-With": "XMLHttpRequest",
		},
		form=None,
		timeout=_FETCH_TIMEOUT_MS,
		fail_on_status_code=False,
		max_retries=0,
	)
	mock_response.dispose.assert_called_once_with()
	mock_page.evaluate.assert_not_called()


def test_safe_browser_context_request_posts_urlencoded_form_once():
	"""写请求使用一次表单 POST，失败时没有 evaluate 或其他通道回退。"""
	session = BrowserSession(cookies={"wt2": "abc"}, user_agent="UA", delay=(0, 0))
	session._started = True
	session._is_persistent = True
	session._commit_ready = True
	session._page = MagicMock(url=HOME_URL)
	session._context = MagicMock()
	mock_response = MagicMock()
	mock_response.json.return_value = {"code": 0, "zpData": {}}
	session._context.request.fetch.return_value = mock_response
	url = endpoints.GREET_URL

	result = session.request(
		"POST",
		url,
		data={"securityId": "temporary", "jobId": 123, "greeting": "您好", "flag": True},
	)

	assert result["code"] == 0
	session._context.request.fetch.assert_called_once_with(
		url,
		method="POST",
		params=None,
		headers={
			"Accept": "application/json, text/plain, */*",
			"Referer": endpoints.REFERER_MAP[url],
			"X-Requested-With": "XMLHttpRequest",
			"Content-Type": "application/x-www-form-urlencoded",
		},
		form={"securityId": "temporary", "jobId": "123", "greeting": "您好", "flag": "true"},
		timeout=_FETCH_TIMEOUT_MS,
		fail_on_status_code=False,
		max_retries=0,
	)
	mock_response.dispose.assert_called_once_with()
	session._page.evaluate.assert_not_called()


def test_safe_browser_context_request_timeout_is_stable_browser_hung():
	session = BrowserSession(cookies={}, user_agent="UA", delay=(0, 0))
	session._started = True
	session._is_persistent = True
	session._commit_ready = True
	session._page = MagicMock(url=HOME_URL)
	session._context = MagicMock()
	session._context.request.fetch.side_effect = PatchrightTimeoutError("timed out")

	with pytest.raises(BrowserStageError) as error:
		session.request("GET", endpoints.SEARCH_URL)

	assert error.value.code == "BROWSER_HUNG"
	assert error.value.stage == "context_request"
	session._context.request.fetch.assert_called_once()
	session._page.evaluate.assert_not_called()


def test_safe_browser_context_request_non_json_is_stable_browser_error():
	session = BrowserSession(cookies={}, user_agent="UA", delay=(0, 0))
	session._started = True
	session._is_persistent = True
	session._commit_ready = True
	session._page = MagicMock(url=HOME_URL)
	session._context = MagicMock()
	mock_response = MagicMock()
	mock_response.json.side_effect = ValueError("not json")
	mock_response.dispose.side_effect = OSError("dispose failed")
	session._context.request.fetch.return_value = mock_response

	with pytest.raises(BrowserStageError) as error:
		session.request("GET", endpoints.SEARCH_URL)

	assert error.value.code == "BROWSER_ERROR"
	assert error.value.stage == "context_request"
	mock_response.dispose.assert_called_once_with()
	session._context.request.fetch.assert_called_once()
	session._page.evaluate.assert_not_called()


def test_safe_browser_commit_failure_never_evaluates(tmp_path):
	"""连 commit 都未完成时没有可用同源上下文，必须拒绝调用。"""
	with patch.dict(
		os.environ,
		{
			"BOSS_SAFE_ISOLATED_BROWSER": "1",
			"BOSS_SAFE_BROWSER_PROFILE": str(tmp_path / "browser-profile"),
		},
	):
		session = BrowserSession(cookies={"wt2": "abc"}, user_agent="UA")
	session._pw = MagicMock()
	mock_context = MagicMock()
	mock_page = MagicMock()
	mock_page.goto.side_effect = RuntimeError("commit timeout")
	mock_context.new_page.return_value = mock_page
	session._pw.chromium.launch_persistent_context.return_value = mock_context

	with pytest.raises(BrowserPageNotReady) as error:
		session._start_headless()

	assert error.value.stage == "navigate_commit"
	mock_page.evaluate.assert_not_called()


def test_safe_browser_commit_to_wrong_host_never_evaluates(tmp_path):
	"""commit 后必须仍是精确 BOSS 域名，不能把技术模式变成跨站调用。"""
	with patch.dict(
		os.environ,
		{
			"BOSS_SAFE_ISOLATED_BROWSER": "1",
			"BOSS_SAFE_BROWSER_PROFILE": str(tmp_path / "browser-profile"),
		},
	):
		session = BrowserSession(cookies={"wt2": "abc"}, user_agent="UA")
	session._pw = MagicMock()
	mock_context = MagicMock()
	mock_page = MagicMock()
	mock_page.url = "https://example.invalid/"
	mock_context.new_page.return_value = mock_page
	session._pw.chromium.launch_persistent_context.return_value = mock_context

	with pytest.raises(BrowserPageNotReady) as error:
		session._start_headless()

	assert error.value.stage == "navigate_commit"
	mock_page.evaluate.assert_not_called()


def test_ensure_started_cleans_playwright_state_after_cold_start_failure():
	"""冷启动失败必须 stop Playwright，避免污染后续搜索请求。"""
	session = BrowserSession(cookies={}, user_agent="")
	mock_pw = MagicMock()

	with (
		patch("boss_agent_cli.api.browser_client.sync_playwright") as mock_sync_playwright,
		patch.object(session, "_try_bridge", return_value=False),
		patch.object(session, "_try_cdp", return_value=False),
		patch.object(session, "_start_headless", side_effect=RuntimeError("cold start failed")),
	):
		mock_sync_playwright.return_value.start.return_value = mock_pw
		try:
			session._ensure_started()
		except RuntimeError as exc:
			assert str(exc) == "cold start failed"
		else:
			raise AssertionError("expected cold-start failure")

	assert session._started is False
	mock_pw.stop.assert_called_once()


def test_ensure_started_falls_back_to_patchright_when_bridge_and_cdp_fail():
	session = BrowserSession(cookies={}, user_agent="")
	mock_pw = MagicMock()
	sentinel = {"headless_started": False}

	def mark_headless_started():
		sentinel["headless_started"] = True
		session._started = True

	with (
		patch.object(session, "_try_bridge", return_value=False) as mock_try_bridge,
		patch("boss_agent_cli.api.browser_client.sync_playwright") as mock_sync_playwright,
		patch.object(session, "_try_cdp", return_value=False) as mock_try_cdp,
		patch.object(session, "_start_headless", side_effect=mark_headless_started) as mock_start_headless,
	):
		mock_sync_playwright.return_value.start.return_value = mock_pw

		session._ensure_started()

	assert sentinel["headless_started"] is True
	assert session._started is True
	assert session._pw is mock_pw
	mock_try_bridge.assert_called_once()
	mock_sync_playwright.assert_called_once()
	mock_try_cdp.assert_called_once()
	mock_start_headless.assert_called_once()


def test_try_cdp_attempts_http_ws_and_devtools_urls_before_falling_back():
	session = BrowserSession(cookies={}, user_agent="", cdp_url="http://127.0.0.1:9333")

	with (
		patch.object(session, "_try_connect", return_value=False) as mock_try_connect,
		patch.object(
			BrowserSession, "_fetch_ws_url", side_effect=[None, "ws://127.0.0.1:9222/devtools/browser/default"]
		) as mock_fetch_ws_url,
		patch.object(
			BrowserSession, "_read_devtools_active_port", return_value="ws://127.0.0.1:9222/devtools/browser/file"
		) as mock_read_port,
	):
		result = session._try_cdp()

	assert result is False
	mock_read_port.assert_called_once()
	assert [call.args[0] for call in mock_try_connect.call_args_list] == [
		"http://127.0.0.1:9333",
		CDP_DEFAULT_URL,
		"ws://127.0.0.1:9222/devtools/browser/default",
		"ws://127.0.0.1:9222/devtools/browser/file",
	]
	assert [call.args[0] for call in mock_fetch_ws_url.call_args_list] == [
		"http://127.0.0.1:9333",
		CDP_DEFAULT_URL,
	]


def test_request_returns_browser_evaluation_json_and_marks_throttle():
	session = BrowserSession(cookies={}, user_agent="")
	session._started = True
	session._page = MagicMock()
	session._page.url = HOME_URL
	session._page.wait_for_load_state.return_value = None
	session._throttle = MagicMock()
	expected = {"code": 0, "zpData": {"jobs": []}}
	session._page.evaluate.return_value = expected

	result = session.request(
		"POST",
		"https://www.zhipin.com/wapi/zpgeek/search/joblist.json",
		params={"query": "python", "page": 1},
		data={"city": "101020100"},
	)

	assert result == expected
	session._throttle.wait.assert_called_once()
	session._throttle.mark.assert_called_once()
	evaluate_script, evaluate_args = session._page.evaluate.call_args.args
	assert "fetch(fetchUrl, options)" in evaluate_script
	assert "AbortController" in evaluate_script
	assert evaluate_args == {
		"method": "POST",
		"url": "https://www.zhipin.com/wapi/zpgeek/search/joblist.json",
		"params": {"query": "python", "page": 1},
		"data": {"city": "101020100"},
		"referer": "https://www.zhipin.com/web/geek/job",
		"requestTimeoutMs": _FETCH_TIMEOUT_MS,
	}


def test_request_rejects_reused_page_while_navigation_is_incomplete():
	"""复用旧 zhipin 页签也必须完成 domcontentloaded 后才能 evaluate。"""
	session = BrowserSession(cookies={}, user_agent="")
	session._started = True
	session._page = MagicMock()
	session._page.url = HOME_URL
	session._page.wait_for_load_state.side_effect = RuntimeError("navigation still running")

	with pytest.raises(BrowserPageNotReady) as error:
		session.request("GET", "https://www.zhipin.com/wapi/zpgeek/search/joblist.json")

	assert error.value.stage == "request_page_ready"
	session._page.evaluate.assert_not_called()


def test_cold_start_wait_constants_stay_lean():
	"""回归护栏：冷启动两处等待须保持精简（曾各浪费 ~3s/搜索）。

	- 自动探测默认 localhost:9222 的超时必须短于显式 --cdp-url 的超时；
	- headless 首页 networkidle 宽限须 <=1s（zhipin 首页基本进不了 networkidle，
	  domcontentloaded 后 JS 环境即可发请求，长宽限是纯浪费）。
	"""
	from boss_agent_cli.api.browser_client import (
		_CDP_AUTO_PROBE_TIMEOUT,
		_CDP_PROBE_TIMEOUT,
		_HEADLESS_NETWORKIDLE_GRACE_MS,
	)

	assert _CDP_AUTO_PROBE_TIMEOUT < _CDP_PROBE_TIMEOUT
	assert _CDP_AUTO_PROBE_TIMEOUT <= 1
	assert _HEADLESS_NETWORKIDLE_GRACE_MS <= 1000


def test_fetch_ws_url_honors_short_auto_probe_timeout():
	"""默认自动探测应以短超时调用 httpx，避免无调试端口时白等。"""
	with patch("httpx.get") as mock_get:
		mock_get.return_value = MagicMock(json=lambda: {"webSocketDebuggerUrl": "ws://x"})
		BrowserSession._fetch_ws_url("http://127.0.0.1:9222", timeout=1)
		assert mock_get.call_args.kwargs["timeout"] == 1


# ── issue #334: CDP 复用已打开的 zhipin 页签，不再每次新开+导航 ─────────────


class _FakePage:
	def __init__(self, url):
		self.url = url


def test_is_zhipin_url_uses_exact_hostname():
	assert _is_zhipin_url("https://www.zhipin.com/web/geek/job")
	assert _is_zhipin_url("https://ZHIPIN.COM./")  # 大小写 + 尾点归一化
	assert not _is_zhipin_url("https://zhipin.com.evil.example/web/geek/job")
	assert not _is_zhipin_url("https://evil.example/?next=https://www.zhipin.com/")
	assert not _is_zhipin_url("not-a-url-with-zhipin.com")


def test_find_reusable_zhipin_page_prefers_open_zhipin_tab():
	ctx = MagicMock()
	other = _FakePage("https://mail.google.com/")
	fake = _FakePage("https://zhipin.com.evil.example/web/geek/job")
	real = _FakePage("https://www.zhipin.com/web/geek/recommend")
	ctx.pages = [other, fake, real]

	assert _find_reusable_zhipin_page(ctx) is real


def test_find_reusable_zhipin_page_none_when_no_zhipin_tab():
	ctx = MagicMock()
	ctx.pages = [_FakePage("https://mail.google.com/")]
	assert _find_reusable_zhipin_page(ctx) is None


def test_try_connect_reuses_open_zhipin_page_without_new_tab_or_nav():
	"""CDP 复用 context 且已有 zhipin 页签时：不新开 page、不导航（修 #334）。"""
	session = BrowserSession(cookies={}, user_agent="")
	session._pw = MagicMock()

	mock_browser = MagicMock()
	mock_user_context = MagicMock()
	open_zhipin_page = _FakePage("https://www.zhipin.com/web/geek/job")
	mock_user_context.pages = [open_zhipin_page]
	mock_browser.contexts = [mock_user_context]
	session._pw.chromium.connect_over_cdp.return_value = mock_browser

	result = session._try_connect("ws://localhost:9222/test")

	assert result is True
	assert session._is_cdp is True
	assert session._own_context is False
	assert session._own_page is False  # 复用用户页签
	assert session._page is open_zhipin_page
	mock_user_context.new_page.assert_not_called()  # 不新开标签页
	assert open_zhipin_page.url == "https://www.zhipin.com/web/geek/job"  # 未被导航覆盖


def test_try_connect_creates_page_when_no_open_zhipin_tab():
	"""CDP 复用 context 但无 zhipin 页签时：仍新建 page 并导航（回退现状）。"""
	session = BrowserSession(cookies={}, user_agent="")
	session._pw = MagicMock()

	mock_browser = MagicMock()
	mock_user_context = MagicMock()
	mock_user_context.pages = [_FakePage("https://mail.google.com/")]
	mock_new_page = MagicMock()
	mock_user_context.new_page.return_value = mock_new_page
	mock_browser.contexts = [mock_user_context]
	session._pw.chromium.connect_over_cdp.return_value = mock_browser

	result = session._try_connect("ws://localhost:9222/test")

	assert result is True
	assert session._own_page is True
	mock_user_context.new_page.assert_called_once()
	mock_new_page.goto.assert_called_once_with(HOME_URL, wait_until="commit", timeout=_NAV_TIMEOUT_MS)


def test_close_does_not_close_reused_user_page():
	"""CDP 复用用户页签（_own_page=False）时 close() 不关闭该页。"""
	session = BrowserSession(cookies={}, user_agent="")
	session._is_cdp = True
	session._own_context = False
	session._own_page = False  # 复用用户页签
	session._started = True
	session._page = MagicMock()
	session._context = MagicMock()
	session._pw = MagicMock()

	session.close()

	session._page.close.assert_not_called()  # 不关用户的页签
	session._context.close.assert_not_called()


# ── boss-safe 专用 Edge 严格 CDP ───────────────────────────────────────────


def test_strict_cdp_reuses_ready_boss_page_without_context_or_request_fallback():
	with patch.dict(
		os.environ,
		{"BOSS_SAFE_ISOLATED_BROWSER": "1", "BOSS_SAFE_STRICT_CDP_URL": "http://127.0.0.1:9222"},
	):
		session = BrowserSession(cookies={"wt2": "abc"}, user_agent="UA", delay=(0, 0))
	session._pw = MagicMock()
	page = MagicMock()
	page.url = HOME_URL
	context = MagicMock()
	context.pages = [page]
	browser = MagicMock()
	browser.contexts = [context]
	session._pw.chromium.connect_over_cdp.return_value = browser

	session._connect_strict_cdp("http://127.0.0.1:9222")

	assert session._is_cdp is True
	assert session._strict_cdp is True
	assert session._page is page
	assert session._own_page is False
	assert session.readiness_stage == "page_ready"
	page.wait_for_load_state.assert_called_once_with(
		"domcontentloaded", timeout=_SAFE_DOMCONTENTLOADED_GRACE_MS
	)
	browser.new_context.assert_not_called()
	context.new_page.assert_not_called()
	context.request.fetch.assert_not_called()


def test_strict_cdp_creates_one_ready_page_when_no_boss_tab_exists():
	with patch.dict(
		os.environ,
		{"BOSS_SAFE_ISOLATED_BROWSER": "1", "BOSS_SAFE_STRICT_CDP_URL": "http://127.0.0.1:9222"},
	):
		session = BrowserSession(cookies={}, user_agent="UA")
	session._pw = MagicMock()
	context = MagicMock()
	context.pages = [_FakePage("https://example.invalid/")]
	page = MagicMock()
	page.url = HOME_URL
	context.new_page.return_value = page
	browser = MagicMock()
	browser.contexts = [context]
	session._pw.chromium.connect_over_cdp.return_value = browser

	session._connect_strict_cdp("http://127.0.0.1:9222")

	context.new_page.assert_called_once_with()
	page.goto.assert_called_once_with(HOME_URL, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
	assert session._page is page
	assert session._own_page is True


def test_strict_cdp_security_page_aborts_without_new_page_or_evaluate():
	with patch.dict(
		os.environ,
		{"BOSS_SAFE_ISOLATED_BROWSER": "1", "BOSS_SAFE_STRICT_CDP_URL": "http://127.0.0.1:9222"},
	):
		session = BrowserSession(cookies={}, user_agent="UA")
	session._pw = MagicMock()
	security_page = MagicMock()
	security_page.url = "https://www.zhipin.com/web/common/security-check.html"
	context = MagicMock()
	context.pages = [security_page]
	browser = MagicMock()
	browser.contexts = [context]
	session._pw.chromium.connect_over_cdp.return_value = browser

	with pytest.raises(BrowserStageError) as error:
		session._connect_strict_cdp("http://127.0.0.1:9222")

	assert error.value.code == "CAPTCHA"
	assert error.value.stage == "page_ready"
	context.new_page.assert_not_called()
	security_page.evaluate.assert_not_called()


def test_strict_cdp_dom_timeout_never_evaluates_or_falls_back():
	with patch.dict(
		os.environ,
		{"BOSS_SAFE_ISOLATED_BROWSER": "1", "BOSS_SAFE_STRICT_CDP_URL": "http://127.0.0.1:9222"},
	):
		session = BrowserSession(cookies={}, user_agent="UA", delay=(0, 0))
	session._started = True
	session._is_cdp = True
	page = MagicMock()
	page.url = HOME_URL
	page.wait_for_load_state.side_effect = RuntimeError("navigation remains pending")
	session._page = page
	session._context = MagicMock()

	with pytest.raises(BrowserPageNotReady) as error:
		session.request("GET", endpoints.SEARCH_URL, params={"query": "AI Agent"})

	assert error.value.stage == "request_page_ready"
	page.evaluate.assert_not_called()
	session._context.request.fetch.assert_not_called()


def test_strict_cdp_page_fetch_get_and_form_post_never_use_api_request_context():
	with patch.dict(
		os.environ,
		{"BOSS_SAFE_ISOLATED_BROWSER": "1", "BOSS_SAFE_STRICT_CDP_URL": "http://127.0.0.1:9222"},
	):
		session = BrowserSession(cookies={}, user_agent="UA", delay=(0, 0))
	session._started = True
	session._is_cdp = True
	page = MagicMock()
	page.url = HOME_URL
	page.evaluate.side_effect = [
		{"code": 0, "zpData": {"jobList": []}},
		{"code": 0, "zpData": {}},
	]
	session._page = page
	session._context = MagicMock()

	search = session.request("GET", endpoints.SEARCH_URL, params={"query": "AI Agent", "page": 1})
	greet = session.request(
		"POST",
		endpoints.GREET_URL,
		data={"securityId": "temporary", "jobId": "job-1", "greeting": "您好"},
	)

	assert search["code"] == 0
	assert greet["code"] == 0
	assert page.evaluate.call_count == 2
	first_script, first_args = page.evaluate.call_args_list[0].args
	second_script, second_args = page.evaluate.call_args_list[1].args
	assert "AbortController" in first_script
	assert "fetch(fetchUrl, options)" in first_script
	assert first_args["method"] == "GET"
	assert first_args["params"] == {"query": "AI Agent", "page": 1}
	assert "URLSearchParams" in second_script
	assert second_args["method"] == "POST"
	assert second_args["data"]["jobId"] == "job-1"
	session._context.request.fetch.assert_not_called()


@pytest.mark.parametrize(
	("evaluate_result", "expected_code"),
	[
		({"code": -1, "message": "BROWSER_FETCH_TIMEOUT_15000MS", "zpData": {}}, "BROWSER_HUNG"),
		({"code": -1, "message": "BROWSER_FETCH_ERROR", "zpData": {}}, "BROWSER_ERROR"),
		(["not", "json", "object"], "BROWSER_ERROR"),
	],
)
def test_strict_cdp_page_fetch_internal_failures_have_stable_codes(evaluate_result, expected_code):
	with patch.dict(
		os.environ,
		{"BOSS_SAFE_ISOLATED_BROWSER": "1", "BOSS_SAFE_STRICT_CDP_URL": "http://127.0.0.1:9222"},
	):
		session = BrowserSession(cookies={}, user_agent="UA", delay=(0, 0))
	session._started = True
	session._is_cdp = True
	session._page = MagicMock(url=HOME_URL)
	session._page.evaluate.return_value = evaluate_result
	session._context = MagicMock()

	with pytest.raises(BrowserStageError) as error:
		session.request("GET", endpoints.SEARCH_URL)

	assert error.value.code == expected_code
	assert error.value.stage == "page_fetch"
	session._page.evaluate.assert_called_once()
	session._context.request.fetch.assert_not_called()


def test_strict_cdp_attach_failure_never_calls_bridge_headless_or_context_request():
	with patch.dict(
		os.environ,
		{"BOSS_SAFE_ISOLATED_BROWSER": "1", "BOSS_SAFE_STRICT_CDP_URL": "http://127.0.0.1:9222"},
	):
		session = BrowserSession(cookies={}, user_agent="UA")
	mock_pw = MagicMock()
	mock_pw.chromium.connect_over_cdp.side_effect = RuntimeError("attach failed")

	with (
		patch("boss_agent_cli.api.browser_client.sync_playwright") as mock_sync_playwright,
		patch.object(session, "_try_bridge") as try_bridge,
		patch.object(session, "_start_headless") as start_headless,
		pytest.raises(BrowserStageError) as error,
	):
		mock_sync_playwright.return_value.start.return_value = mock_pw
		session._ensure_started()

	assert error.value.code == "BROWSER_ERROR"
	assert error.value.stage == "cdp_attach"
	try_bridge.assert_not_called()
	start_headless.assert_not_called()
	mock_pw.chromium.launch.assert_not_called()
