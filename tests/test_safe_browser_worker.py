"""隔离浏览器 worker 的离线回收契约测试。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

import boss_agent_cli.safe_browser_worker as worker_module
from boss_agent_cli.api.browser_client import BrowserPageNotReady, BrowserStageError
from boss_agent_cli.safe_browser_worker import BrowserWorkerError, SafeBrowserPlatform, SafeBrowserWorker


class _FakeConnection:
	def __init__(self, responses: list[dict[str, Any]], *, interrupt: bool = False) -> None:
		self.responses = list(responses)
		self.sent: list[dict[str, Any]] = []
		self.interrupt = interrupt
		self.closed = False

	def send(self, payload: dict[str, Any]) -> None:
		self.sent.append(payload)

	def poll(self, timeout: float) -> bool:
		if self.responses:
			return True
		if self.interrupt:
			raise KeyboardInterrupt()
		time.sleep(timeout)
		return False

	def recv(self) -> dict[str, Any]:
		return self.responses.pop(0)

	def close(self) -> None:
		self.closed = True


class _FakeProcess:
	pid = 4242

	def __init__(self) -> None:
		self.alive = True
		self.started = False
		self.terminated = False
		self.killed = False

	def start(self) -> None:
		self.started = True

	def is_alive(self) -> bool:
		return self.alive

	def terminate(self) -> None:
		self.terminated = True
		self.alive = False

	def kill(self) -> None:
		self.killed = True
		self.alive = False

	def join(self, timeout: float | None = None) -> None:
		return None


class _FakeContext:
	def __init__(self, connection: _FakeConnection, process: _FakeProcess) -> None:
		self.connection = connection
		self.process = process

	def Pipe(self, duplex: bool = True) -> tuple[_FakeConnection, _FakeConnection]:
		return self.connection, _FakeConnection([])

	def Process(self, **kwargs: Any) -> _FakeProcess:
		return self.process


def _patch_process(monkeypatch: pytest.MonkeyPatch, connection: _FakeConnection) -> _FakeProcess:
	process = _FakeProcess()
	context = _FakeContext(connection, process)
	monkeypatch.setattr(worker_module.multiprocessing, "get_context", lambda name: context)
	monkeypatch.setattr(worker_module, "_windows_process_snapshot", lambda: [])
	def taskkill(pid: int, tree: bool) -> None:
		if pid == process.pid:
			process.alive = False
	monkeypatch.setattr(worker_module, "_taskkill_pid", taskkill)
	monkeypatch.setattr(worker_module, "_pid_alive", lambda pid: False)
	return process


def test_safe_browser_platform_exposes_read_only_user_info_operation() -> None:
	class StubWorker:
		def __init__(self) -> None:
			self.calls: list[tuple[str, dict[str, Any]]] = []

		def call(self, operation: str, **payload: Any) -> dict[str, Any]:
			self.calls.append((operation, payload))
			return {"code": 0, "zpData": {}}

	platform = object.__new__(SafeBrowserPlatform)
	platform._worker = StubWorker()  # type: ignore[assignment]  # noqa: SLF001

	assert platform.user_info()["code"] == 0
	assert platform._worker.calls == [("user_info", {})]  # type: ignore[union-attr]  # noqa: SLF001


def test_initialization_hard_timeout_recovers_in_injected_short_timeout(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	connection = _FakeConnection([])
	process = _patch_process(monkeypatch, connection)
	monkeypatch.setattr(worker_module, "BROWSER_INIT_TIMEOUT_SECONDS", 0.02)

	started = time.monotonic()
	with pytest.raises(BrowserWorkerError) as error:
		SafeBrowserWorker(tmp_path)

	assert error.value.code == "BROWSER_HUNG"
	assert time.monotonic() - started < 1.0
	assert process.started is True
	assert connection.closed is True


def test_login_post_authentication_hard_timeout_recovers_only_worker(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	connection = _FakeConnection([{"type": "ready"}, {"type": "authenticated"}])
	process = _patch_process(monkeypatch, connection)
	monkeypatch.setattr(worker_module, "BROWSER_CALL_TIMEOUT_SECONDS", 0.02)

	started = time.monotonic()
	with pytest.raises(BrowserWorkerError) as error:
		worker_module.run_login_in_worker(
			tmp_path,
			cdp_url="http://127.0.0.1:9222",
			timeout=30,
		)

	assert error.value.code == "BROWSER_HUNG"
	assert time.monotonic() - started < 1.0
	assert process.alive is False
	diagnostic = (tmp_path / "safe-diagnostics.jsonl").read_text(encoding="utf-8")
	assert '"stage":"call_timeout"' in diagnostic
	assert '"operation":"login"' in diagnostic


def test_login_ctrl_c_recovers_worker_without_waiting_for_login_timeout(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	connection = _FakeConnection([{"type": "ready"}], interrupt=True)
	process = _patch_process(monkeypatch, connection)

	with pytest.raises(BrowserWorkerError) as error:
		worker_module.run_login_in_worker(
			tmp_path,
			cdp_url="http://127.0.0.1:9222",
			timeout=600,
		)

	assert error.value.code == "BROWSER_HUNG"
	assert process.alive is False


def test_permanent_browser_call_timeout_is_recovered_without_second_call(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	connection = _FakeConnection([{"type": "ready"}])
	process = _patch_process(monkeypatch, connection)
	monkeypatch.setattr(worker_module, "BROWSER_CALL_TIMEOUT_SECONDS", 0.02)

	worker = SafeBrowserWorker(tmp_path)
	with pytest.raises(BrowserWorkerError) as error:
		worker.call("search_jobs", query="AI Agent", filters={})

	assert error.value.code == "BROWSER_HUNG"
	assert [item["operation"] for item in connection.sent] == ["search_jobs"]
	assert process.alive is False
	diagnostic = (tmp_path / "safe-diagnostics.jsonl").read_text(encoding="utf-8")
	assert '"stage":"call_timeout"' in diagnostic
	worker.close()


def test_context_does_not_repeat_cleanup_and_mask_call_timeout(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""超时已完成清理后，上下文退出不得二次清理并遮蔽原始错误。"""
	connection = _FakeConnection([{"type": "ready"}])
	_patch_process(monkeypatch, connection)
	monkeypatch.setattr(worker_module, "BROWSER_CALL_TIMEOUT_SECONDS", 0.02)
	original_cleanup = worker_module._cleanup_process_tree
	cleanup_calls = 0

	def cleanup(root_pid: int, profile: Path, process: Any) -> None:
		nonlocal cleanup_calls
		cleanup_calls += 1
		if cleanup_calls > 1:
			raise SystemError("<class 'OSError'> returned a result with an exception set")
		original_cleanup(root_pid, profile, process)

	monkeypatch.setattr(worker_module, "_cleanup_process_tree", cleanup)
	platform = object.__new__(SafeBrowserPlatform)
	platform._worker = SafeBrowserWorker(tmp_path)  # noqa: SLF001

	with pytest.raises(BrowserWorkerError) as error:
		with platform:
			platform.search_jobs("AI Agent")

	assert error.value.code == "BROWSER_HUNG"
	assert cleanup_calls == 1


def test_transient_cleanup_oserror_is_retried_without_masking_call_timeout(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""首次清理的 Windows 底层异常可恢复时，终端仍应保留 `BROWSER_HUNG`。"""
	connection = _FakeConnection([{"type": "ready"}])
	_patch_process(monkeypatch, connection)
	monkeypatch.setattr(worker_module, "BROWSER_CALL_TIMEOUT_SECONDS", 0.02)
	original_cleanup = worker_module._cleanup_process_tree
	cleanup_calls = 0

	def cleanup(root_pid: int, profile: Path, process: Any) -> None:
		nonlocal cleanup_calls
		cleanup_calls += 1
		if cleanup_calls == 1:
			raise SystemError("<class 'OSError'> returned a result with an exception set")
		original_cleanup(root_pid, profile, process)

	monkeypatch.setattr(worker_module, "_cleanup_process_tree", cleanup)
	worker = SafeBrowserWorker(tmp_path)

	with pytest.raises(BrowserWorkerError) as error:
		worker.call("search_jobs", query="AI Agent", filters={})

	assert error.value.code == "BROWSER_HUNG"
	assert cleanup_calls == 2
	worker.close()


def test_repeated_cleanup_failure_takes_priority_as_profile_not_clean(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""精确清理连续失败时不得假装已回收，必须升级安全错误。"""
	connection = _FakeConnection([{"type": "ready"}])
	_patch_process(monkeypatch, connection)
	monkeypatch.setattr(worker_module, "BROWSER_CALL_TIMEOUT_SECONDS", 0.02)
	cleanup_calls = 0

	def cleanup(*_: Any) -> None:
		nonlocal cleanup_calls
		cleanup_calls += 1
		raise SystemError("cleanup failed")

	monkeypatch.setattr(worker_module, "_cleanup_process_tree", cleanup)
	worker = SafeBrowserWorker(tmp_path)

	with pytest.raises(BrowserWorkerError) as error:
		worker.call("search_jobs", query="AI Agent", filters={})

	assert error.value.code == "BROWSER_PROFILE_NOT_CLEAN"
	assert cleanup_calls == 2


def test_pid_alive_treats_windows_systemerror_as_not_alive(monkeypatch: pytest.MonkeyPatch) -> None:
	"""Windows `os.kill(pid, 0)` 的 ctypes 竞态不得泄漏为公共错误。"""
	monkeypatch.setattr(
		worker_module.os,
		"kill",
		lambda *_: (_ for _ in ()).throw(SystemError("<class 'OSError'> returned a result with an exception set")),
	)

	assert worker_module._pid_alive(4242) is False


def test_ctrl_c_aborts_worker_and_does_not_leave_ipc_session(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	connection = _FakeConnection([{"type": "ready"}], interrupt=True)
	process = _patch_process(monkeypatch, connection)

	worker = SafeBrowserWorker(tmp_path)
	with pytest.raises(BrowserWorkerError) as error:
		worker.call("search_jobs", query="AI Agent", filters={})

	assert error.value.code == "BROWSER_HUNG"
	assert process.alive is False
	assert [item["operation"] for item in connection.sent] == ["search_jobs"]
	worker.close()


def test_descendant_selection_is_exact_and_does_not_use_process_names() -> None:
	snapshot = [
		worker_module._ProcessInfo(100, 1, "worker"),
		worker_module._ProcessInfo(101, 100, "chromium --user-data-dir=safe"),
		worker_module._ProcessInfo(102, 101, "gpu-process"),
		worker_module._ProcessInfo(200, 1, "other chromium"),
	]

	assert worker_module._descendant_pids(100, snapshot) == {100, 101, 102}


def test_diagnostics_have_only_allowed_fields(tmp_path: Path) -> None:
	worker_module._diagnostic(
		tmp_path,
		stage="call",
		operation="greet",
		started=time.monotonic(),
		error_code="BROWSER_HUNG",
	)
	line = (tmp_path / "safe-diagnostics.jsonl").read_text(encoding="utf-8").strip()
	assert set(__import__("json").loads(line)) == {"stage", "operation", "elapsed_ms", "error_code"}


def test_browser_page_stage_is_whitelisted_before_diagnostic() -> None:
	exc = BrowserPageNotReady("navigate_commit_only")
	assert worker_module._worker_exception_stage(exc, "init") == "navigate_commit_only"

	unsafe = RuntimeError("secret")
	unsafe.stage = "cookie=secret"  # type: ignore[attr-defined]
	assert worker_module._worker_exception_stage(unsafe, "init") == "init"


def test_context_request_timeout_keeps_stable_code_and_stage() -> None:
	exc = BrowserStageError("BROWSER_HUNG", "context_request")

	assert worker_module._worker_exception_code(exc) == "BROWSER_HUNG"
	assert worker_module._worker_exception_stage(exc, "call") == "context_request"


def test_worker_environment_selects_strict_edge_cdp_without_old_profile(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	# setenv 让 monkeypatch 记录原值；直接调用 worker 的环境注入函数后可可靠复原。
	monkeypatch.setenv("BOSS_SAFE_ISOLATED_BROWSER", "")
	monkeypatch.setenv("BOSS_SAFE_BROWSER_PROFILE", "")
	monkeypatch.setenv("BOSS_SAFE_STRICT_CDP_URL", "")
	worker_module._set_worker_environment(tmp_path, "http://127.0.0.1:9222")

	assert worker_module.os.environ["BOSS_SAFE_ISOLATED_BROWSER"] == "1"
	assert worker_module.os.environ["BOSS_SAFE_STRICT_CDP_URL"] == "http://127.0.0.1:9222"
	assert worker_module.os.environ["BOSS_SAFE_BROWSER_PROFILE"] == str(tmp_path / "edge-cdp-profile")


def test_worker_cleanup_does_not_select_persistent_edge_profile_process() -> None:
	snapshot = [
		worker_module._ProcessInfo(100, 1, "boss-safe worker"),
		worker_module._ProcessInfo(101, 100, "patchright node driver"),
		worker_module._ProcessInfo(900, 1, "msedge --user-data-dir=edge-cdp-profile"),
	]

	assert worker_module._descendant_pids(100, snapshot) == {100, 101}
	assert worker_module._profile_process_pids(None, snapshot) == set()


def test_page_fetch_stage_and_platform_challenge_codes_are_whitelisted() -> None:
	hung = BrowserStageError("BROWSER_HUNG", "page_fetch")
	challenge = BrowserStageError("CAPTCHA", "page_ready")

	assert worker_module._worker_exception_code(hung) == "BROWSER_HUNG"
	assert worker_module._worker_exception_stage(hung, "call") == "page_fetch"
	assert worker_module._worker_exception_code(challenge) == "CAPTCHA"
	assert worker_module._worker_exception_stage(challenge, "call") == "page_ready"
