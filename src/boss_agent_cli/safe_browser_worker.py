"""boss-safe 的严格 CDP 浏览器工作进程与本地 IPC。

父进程只负责状态、额度、熔断和交互；Patchright CDP 客户端与页面调用留
在一个短生命周期工作进程内。同步 ``page.evaluate`` 即使永久不返回，也
只能阻塞这个工作进程。专用浏览器由独立运行时管理，worker 回收不会结束它。
"""

from __future__ import annotations

import json
import multiprocessing
import os
import time
from dataclasses import dataclass
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, Iterable, cast

import psutil


BROWSER_INIT_TIMEOUT_SECONDS = 30.0
BROWSER_CALL_TIMEOUT_SECONDS = 25.0
_WORKER_SHUTDOWN_TIMEOUT_SECONDS = 5.0
_DIAGNOSTIC_FILE = "safe-diagnostics.jsonl"
_SAFE_DIAGNOSTIC_STAGES = frozenset({
	"init",
	"call",
	"login",
	"shutdown",
	"context_launch",
	"navigate_commit",
	"navigate_commit_only",
	"navigate_domcontentloaded",
	"reused_page_ready",
	"request_page_ready",
	"context_request",
	"edge_launch",
	"edge_shutdown",
	"browser_launch",
	"browser_shutdown",
	"browser_port",
	"cdp_attach",
	"page_ready",
	"page_fetch",
	"call_timeout",
})


class BrowserWorkerError(RuntimeError):
	"""父进程可识别的稳定浏览器错误。"""

	def __init__(self, code: str, operation: str, *, fatal: bool = True) -> None:
		self.code = code
		self.operation = operation
		self.fatal = fatal
		super().__init__(f"{code}: {operation}")


@dataclass(frozen=True, slots=True)
class _ProcessInfo:
	pid: int
	parent_pid: int
	command_line: str
	creation_time: str = ""


def _diagnostic(data_dir: Path, *, stage: str, operation: str, started: float, error_code: str) -> None:
	"""只记录阶段、耗时、操作名和稳定错误码。"""
	record = {
		"stage": stage,
		"operation": operation,
		"elapsed_ms": round((time.monotonic() - started) * 1000, 1),
		"error_code": error_code,
	}
	try:
		with (data_dir / _DIAGNOSTIC_FILE).open("a", encoding="utf-8") as handle:
			handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
	except OSError:
		# 诊断不能反过来阻塞安全流程。
		pass


def record_diagnostic(data_dir: Path, *, stage: str, operation: str, started: float, error_code: str) -> None:
	"""供父进程记录浏览器生命周期阶段；字段仍由同一白名单格式约束。"""
	if stage not in _SAFE_DIAGNOSTIC_STAGES:
		stage = "call"
	_diagnostic(data_dir, stage=stage, operation=operation, started=started, error_code=error_code)


def _set_worker_environment(data_dir: Path, cdp_url: str) -> None:
	from boss_agent_cli.safe_edge import browser_profile_path

	os.environ["BOSS_SAFE_ISOLATED_BROWSER"] = "1"
	os.environ["BOSS_SAFE_BROWSER_PROFILE"] = str(browser_profile_path(data_dir))
	os.environ["BOSS_SAFE_STRICT_CDP_URL"] = cdp_url


def _worker_exception_code(exc: BaseException) -> str:
	from boss_agent_cli.api.browser_client import BrowserPageNotReady, BrowserStageError
	from boss_agent_cli.api.client import AccountRiskError
	from boss_agent_cli.auth.manager import AuthRequired, TokenRefreshFailed
	from boss_agent_cli.safe_edge import BrowserRuntimeError

	if isinstance(exc, BrowserPageNotReady):
		return "BROWSER_PAGE_NOT_READY"
	if isinstance(exc, BrowserRuntimeError):
		return exc.code
	if isinstance(exc, BrowserStageError):
		return exc.code if exc.code in {
			"BROWSER_HUNG",
			"BROWSER_ERROR",
			"BROWSER_PAGE_NOT_READY",
			"ACCOUNT_RISK",
			"CAPTCHA",
		} else "BROWSER_ERROR"
	if isinstance(exc, AccountRiskError):
		return "ACCOUNT_RISK"
	if isinstance(exc, AuthRequired):
		return "AUTH_REQUIRED"
	if isinstance(exc, TokenRefreshFailed):
		return "TOKEN_REFRESH_FAILED"
	if isinstance(exc, TimeoutError):
		return "BROWSER_HUNG"
	return "BROWSER_ERROR"


def _worker_exception_stage(exc: BaseException, default: str) -> str:
	stage = str(getattr(exc, "stage", "") or "")
	return stage if stage in _SAFE_DIAGNOSTIC_STAGES else default


def _force_browser_initialization(platform: Any) -> str:
	client = getattr(platform, "_client", None)
	get_browser = getattr(client, "_get_browser", None)
	if not callable(get_browser):
		raise RuntimeError("browser client unavailable")
	browser = get_browser()
	ensure_started = getattr(browser, "_ensure_started", None)
	if not callable(ensure_started):
		raise RuntimeError("browser session unavailable")
	ensure_started()
	stage = str(getattr(browser, "readiness_stage", "") or "")
	return stage if stage in _SAFE_DIAGNOSTIC_STAGES else "init"


def _send(connection: Connection, payload: dict[str, Any]) -> bool:
	try:
		connection.send(payload)
		return True
	except (BrokenPipeError, EOFError, OSError):
		return False


def _worker_main(connection: Connection, data_dir_text: str, delay: tuple[float, float]) -> None:
	"""工作进程入口；不把异常正文或平台完整响应写入诊断。"""
	data_dir = Path(data_dir_text)
	platform: Any = None
	init_started = time.monotonic()
	try:
		from boss_agent_cli.safe_edge import strict_cdp_url

		cdp_url = strict_cdp_url(data_dir)
		_set_worker_environment(data_dir, cdp_url)
		from boss_agent_cli.auth.manager import AuthManager
		from boss_agent_cli.commands._platform import build_platform_instance
		from boss_agent_cli.output import Logger

		auth = AuthManager(data_dir, logger=Logger("error"), platform="zhipin")
		platform = build_platform_instance("zhipin", auth, delay=delay, cdp_url=cdp_url)
		readiness_stage = _force_browser_initialization(platform)
		_diagnostic(data_dir, stage=readiness_stage, operation="browser", started=init_started, error_code="OK")
		if not _send(connection, {"type": "ready"}):
			return

		while True:
			try:
				request = connection.recv()
			except (EOFError, OSError):
				return
			if not isinstance(request, dict):
				continue
			operation = str(request.get("operation", ""))
			started = time.monotonic()
			try:
				if operation == "shutdown":
					if _send(connection, {"type": "result", "operation": operation, "result": None}):
						_diagnostic(data_dir, stage="shutdown", operation=operation, started=started, error_code="OK")
					return
				if operation == "search_jobs":
					query = str(request.get("query", ""))
					filters = request.get("filters", {})
					if not isinstance(filters, dict):
						filters = {}
					result = platform.search_jobs(query, **filters)
				elif operation == "user_info":
					result = platform.user_info()
				elif operation == "greet":
					result = platform.greet(
						str(request.get("security_id", "")),
						str(request.get("job_id", "")),
						str(request.get("message", "")),
					)
				else:
					raise ValueError("unsupported browser operation")
				_diagnostic(data_dir, stage="call", operation=operation, started=started, error_code="OK")
				_send(connection, {"type": "result", "operation": operation, "result": result})
			except BaseException as exc:
				code = _worker_exception_code(exc)
				stage = _worker_exception_stage(exc, "call")
				_diagnostic(data_dir, stage=stage, operation=operation, started=started, error_code=code)
				if not _send(connection, {"type": "error", "operation": operation, "code": code}):
					return
	except BaseException as exc:
		code = _worker_exception_code(exc)
		stage = _worker_exception_stage(exc, "init")
		_diagnostic(data_dir, stage=stage, operation="browser", started=init_started, error_code=code)
		_send(connection, {"type": "error", "operation": "browser", "code": code})
	finally:
		if platform is not None:
			try:
				platform.close()
			except BaseException:
				pass
		try:
			connection.close()
		except OSError:
			pass


def _login_worker_main(connection: Connection, data_dir_text: str, cdp_url: str, timeout: int, platform: str) -> None:
	"""登录在独立客户端进程中附着常驻专用浏览器，父进程只接收结果。"""
	data_dir = Path(data_dir_text)
	started = time.monotonic()
	try:
		_set_worker_environment(data_dir, cdp_url)
		from boss_agent_cli.auth.browser import login_via_cdp
		from boss_agent_cli.safe_edge import strict_cdp_user_agent

		def notify_ready() -> None:
			_send(connection, {"type": "ready"})

		def notify_authenticated() -> None:
			_send(connection, {"type": "authenticated"})

		token = login_via_cdp(
			cdp_url=cdp_url,
			timeout=timeout,
			platform=platform,
			ready_callback=notify_ready,
			authenticated_callback=notify_authenticated,
			user_agent_override=strict_cdp_user_agent(data_dir),
			keep_page=True,
		)
		_diagnostic(data_dir, stage="login", operation="login", started=started, error_code="OK")
		_send(connection, {"type": "result", "operation": "login", "result": token})
	except BaseException as exc:
		code = "LOGIN_TIMEOUT" if isinstance(exc, TimeoutError) else _worker_exception_code(exc)
		_diagnostic(data_dir, stage="login", operation="login", started=started, error_code=code)
		_send(connection, {"type": "error", "operation": "login", "code": code})
	finally:
		try:
			connection.close()
		except OSError:
			pass


def _wait_login_response(connection: Any, process: Any, operation: str, timeout: float) -> dict[str, Any]:
	deadline = time.monotonic() + timeout
	try:
		while True:
			remaining = deadline - time.monotonic()
			if remaining <= 0:
				raise BrowserWorkerError("BROWSER_HUNG", operation)
			if not connection.poll(min(0.25, remaining)):
				if not process.is_alive():
					raise BrowserWorkerError("BROWSER_HUNG", operation)
				continue
			response = connection.recv()
			if not isinstance(response, dict):
				continue
			if response.get("type") == "error":
				raise BrowserWorkerError(str(response.get("code") or "BROWSER_ERROR"), operation, fatal=False)
			if response.get("type") in {"ready", "authenticated", "result"}:
				return response
	except KeyboardInterrupt as exc:
		raise BrowserWorkerError("BROWSER_HUNG", operation) from exc
	except (EOFError, OSError, SystemError) as exc:
		raise BrowserWorkerError("BROWSER_HUNG", operation) from exc


def run_login_in_worker(
	data_dir: Path,
	*,
	cdp_url: str,
	timeout: int,
	platform: str = "zhipin",
) -> dict[str, Any]:
	"""在独立 worker 中附着专用浏览器登录；回收 worker，不关闭浏览器。"""
	context = multiprocessing.get_context("spawn")
	parent_connection, child_connection = context.Pipe(duplex=True)
	process = context.Process(
		target=_login_worker_main,
		args=(child_connection, str(data_dir), cdp_url, timeout, platform),
		name="boss-safe-login-worker",
	)
	process.daemon = False
	try:
		process.start()
	finally:
		try:
			child_connection.close()
		except OSError:
			pass

	try:
		ready = _wait_login_response(parent_connection, process, "login", BROWSER_INIT_TIMEOUT_SECONDS)
		if ready.get("type") != "ready":
			raise BrowserWorkerError("BROWSER_ERROR", "login", fatal=False)
		result = _wait_login_response(parent_connection, process, "login", float(timeout) + 5.0)
		if result.get("type") == "authenticated":
			post_auth_started = time.monotonic()
			try:
				result = _wait_login_response(
					parent_connection,
					process,
					"login",
					BROWSER_CALL_TIMEOUT_SECONDS,
				)
			except BrowserWorkerError as exc:
				if exc.code == "BROWSER_HUNG":
					_diagnostic(
						data_dir,
						stage="call_timeout",
						operation="login",
						started=post_auth_started,
						error_code=exc.code,
					)
				raise
		if result.get("type") != "result" or not isinstance(result.get("result"), dict):
			raise BrowserWorkerError("BROWSER_ERROR", "login", fatal=False)
		return cast("dict[str, Any]", result["result"])
	except BaseException:
		try:
			root_pid = int(getattr(process, "pid", 0) or 0)
			if root_pid:
				_cleanup_process_tree(root_pid, None, process)
		except BrowserWorkerError:
			pass
		raise
	finally:
		try:
			parent_connection.close()
		except OSError:
			pass
		try:
			process.join(2)
		except (OSError, SystemError, AttributeError):
			pass


def _windows_process_snapshot() -> list[_ProcessInfo]:
	"""兼容旧名称的跨平台 psutil 快照。"""
	items: list[_ProcessInfo] = []
	for process in psutil.process_iter(["pid", "ppid", "cmdline", "create_time"], ad_value=None):
		try:
			info = process.info
			command = info.get("cmdline") or []
			created = float(info.get("create_time") or 0)
			items.append(_ProcessInfo(
				pid=int(info["pid"]),
				parent_pid=int(info.get("ppid") or 0),
				command_line=" ".join(map(str, command)) if isinstance(command, (list, tuple)) else str(command),
				creation_time=f"{created:.6f}" if created else "",
			))
		except (psutil.NoSuchProcess, psutil.ZombieProcess, OSError, TypeError, ValueError):
			continue
	return items


def _descendant_pids(root_pid: int, snapshot: Iterable[_ProcessInfo]) -> set[int]:
	children: dict[int, set[int]] = {}
	for process in snapshot:
		children.setdefault(process.parent_pid, set()).add(process.pid)
	found = {root_pid}
	frontier = [root_pid]
	while frontier:
		parent = frontier.pop()
		for child in children.get(parent, set()):
			if child not in found:
				found.add(child)
				frontier.append(child)
	return found


def _normal_path(value: str) -> str:
	normalized = os.path.normcase(os.path.normpath(value.replace('"', "")))
	return normalized.casefold() if os.name == "nt" else normalized


def _profile_process_pids(profile: Path | None, snapshot: Iterable[_ProcessInfo]) -> set[int]:
	if profile is None:
		return set()
	needle = _normal_path(str(profile))
	if not needle:
		return set()
	return {
		process.pid
		for process in snapshot
		if needle in _normal_path(process.command_line)
	}


def _taskkill_pid(pid: int, *, tree: bool) -> None:
	if pid <= 0:
		return
	try:
		root = psutil.Process(pid)
		targets = root.children(recursive=True) if tree else []
		targets.append(root)
		for process in reversed(targets):
			try:
				process.terminate()
			except psutil.NoSuchProcess:
				pass
		_, alive = psutil.wait_procs(targets, timeout=2)
		for process in alive:
			try:
				process.kill()
			except psutil.NoSuchProcess:
				pass
	except (psutil.NoSuchProcess, psutil.AccessDenied, OSError, SystemError):
		pass


def _pid_alive(pid: int) -> bool:
	if pid <= 0:
		return False
	try:
		process = psutil.Process(pid)
		return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
	except (psutil.NoSuchProcess, psutil.ZombieProcess, OSError, SystemError):
		return False


def _cleanup_process_tree(root_pid: int, profile: Path | None, process: Any) -> None:
	"""只终止本次 worker 及其已枚举后代，不按名称结束常驻专用浏览器。"""
	before = _windows_process_snapshot()
	known = _descendant_pids(root_pid, before)
	known.update(_profile_process_pids(profile, before))
	try:
		root_alive = bool(process.is_alive())
	except (OSError, SystemError, AttributeError):
		root_alive = False
	if root_alive:
		_taskkill_pid(root_pid, tree=True)
	for pid in sorted(known - {root_pid}):
		if _pid_alive(pid):
			_taskkill_pid(pid, tree=False)
	try:
		process.join(5)
	except (OSError, SystemError, AttributeError):
		pass
	try:
		root_alive = bool(process.is_alive())
	except (OSError, SystemError, AttributeError):
		root_alive = False
	if root_alive:
		_taskkill_pid(root_pid, tree=True)
	try:
		process.join(2)
	except (OSError, SystemError, AttributeError):
		pass

	deadline = time.monotonic() + 3
	while time.monotonic() < deadline:
		after = _windows_process_snapshot()
		alive = {item.pid for item in after}
		residual = (known & alive) | _profile_process_pids(profile, after)
		if not residual:
			return
		for pid in sorted(residual):
			_taskkill_pid(pid, tree=False)
		time.sleep(0.1)
	after = _windows_process_snapshot()
	alive = {item.pid for item in after}
	residual = (known & alive) | _profile_process_pids(profile, after)
	if residual:
		raise BrowserWorkerError("BROWSER_PROFILE_NOT_CLEAN", "cleanup")


class SafeBrowserWorker:
	"""一个命令对应一个严格 CDP worker；常驻专用浏览器不属于其进程树。"""

	def __init__(self, data_dir: Path, *, delay: tuple[float, float] = (2.0, 5.0)) -> None:
		self._data_dir = data_dir
		self._profile: Path | None = None
		self._closed = False
		self._broken = False
		self._aborted = False
		self._cleanup_complete = False
		self._parent_connection: Any = None
		self._process: Any = None
		context = multiprocessing.get_context("spawn")
		parent_connection, child_connection = context.Pipe(duplex=True)
		self._parent_connection = parent_connection
		self._process = context.Process(
			target=_worker_main,
			args=(child_connection, str(data_dir), delay),
			name="boss-safe-browser-worker",
		)
		self._process.daemon = False
		try:
			self._process.start()
		finally:
			try:
				child_connection.close()
			except (OSError, SystemError):
				pass
		try:
			self._wait_for_response("init", BROWSER_INIT_TIMEOUT_SECONDS)
		except BaseException:
			try:
				self._abort_worker()
			except Exception:
				pass
			self._close_connection()
			raise

	def _close_connection(self) -> None:
		if self._parent_connection is not None:
			try:
				self._parent_connection.close()
			except (OSError, SystemError):
				pass
			self._parent_connection = None

	def _wait_for_response(self, operation: str, timeout: float) -> dict[str, Any]:
		connection = self._parent_connection
		if connection is None:
			raise BrowserWorkerError("BROWSER_HUNG", operation)
		deadline = time.monotonic() + timeout
		try:
			while True:
				remaining = deadline - time.monotonic()
				if remaining <= 0:
					raise BrowserWorkerError("BROWSER_HUNG", operation)
				if not connection.poll(min(0.25, remaining)):
					if self._process is not None:
						try:
							alive = bool(self._process.is_alive())
						except (OSError, SystemError, AttributeError):
							alive = False
						if not alive:
							raise BrowserWorkerError("BROWSER_HUNG", operation)
					continue
				response = connection.recv()
				if not isinstance(response, dict):
					continue
				if response.get("type") == "error":
					raise BrowserWorkerError(
					str(response.get("code") or "BROWSER_ERROR"),
					operation,
					fatal=False,
				)
				if response.get("type") in {"ready", "result"}:
					return response
		except KeyboardInterrupt as exc:
			raise BrowserWorkerError("BROWSER_HUNG", operation) from exc
		except (EOFError, OSError, SystemError) as exc:
			raise BrowserWorkerError("BROWSER_HUNG", operation) from exc

	def _cleanup_worker_tree(self) -> None:
		"""清理并验证一次 worker 树；成功后禁止上下文退出重复清理。"""
		if self._cleanup_complete:
			return
		process = self._process
		root_pid = int(getattr(process, "pid", 0) or 0)
		if root_pid:
			_cleanup_process_tree(root_pid, self._profile, process)
		self._cleanup_complete = True

	def _abort_after_primary_error(self, *, started: float) -> None:
		"""回收超时 worker；可恢复的清理竞态不得遮蔽原始错误。"""
		try:
			self._abort_worker()
		except BrowserWorkerError:
			# 确认 profile 仍有残留时，安全错误优先级高于调用超时。
			raise
		except Exception:
			_diagnostic(
				self._data_dir,
				stage="shutdown",
				operation="cleanup",
				started=started,
				error_code="BROWSER_ERROR",
			)
			try:
				self._cleanup_worker_tree()
			except BrowserWorkerError:
				raise
			except Exception as retry_exc:
				raise BrowserWorkerError("BROWSER_PROFILE_NOT_CLEAN", "cleanup") from retry_exc

	def call(self, operation: str, **payload: Any) -> dict[str, Any]:
		if self._closed or self._broken:
			raise BrowserWorkerError("BROWSER_HUNG", operation)
		connection = self._parent_connection
		if connection is None:
			raise BrowserWorkerError("BROWSER_HUNG", operation)
		started = time.monotonic()
		try:
			connection.send({"operation": operation, **payload})
			response = self._wait_for_response(operation, BROWSER_CALL_TIMEOUT_SECONDS)
		except BrowserWorkerError as exc:
			if exc.fatal:
				_diagnostic(
					self._data_dir,
					stage="call_timeout",
					operation=operation,
					started=started,
					error_code=exc.code,
				)
				# 超时、Ctrl+C 或 IPC 断裂后不允许在同一 context 上继续调用。
				self._abort_after_primary_error(started=started)
			raise
		except (BrokenPipeError, EOFError, OSError, SystemError) as exc:
			primary = BrowserWorkerError("BROWSER_HUNG", operation)
			_diagnostic(
				self._data_dir,
				stage="call_timeout",
				operation=operation,
				started=started,
				error_code=primary.code,
			)
			self._abort_after_primary_error(started=started)
			raise primary from exc
		return cast("dict[str, Any]", response.get("result"))

	def _abort_worker(self) -> None:
		if self._cleanup_complete:
			self._broken = True
			return
		self._aborted = True
		self._broken = True
		self._cleanup_worker_tree()

	def close(self) -> None:
		if self._closed:
			return
		self._closed = True
		cleanup_error: BrowserWorkerError | None = None
		if not self._broken and self._parent_connection is not None:
			try:
				self._parent_connection.send({"operation": "shutdown"})
				self._wait_for_response("shutdown", _WORKER_SHUTDOWN_TIMEOUT_SECONDS)
			except BaseException:
				try:
					self._abort_worker()
				except BrowserWorkerError as exc:
					cleanup_error = exc
				except Exception:
					# 下方的统一清理块会精确重试并验证 profile。
					pass
			else:
				try:
					self._process.join(_WORKER_SHUTDOWN_TIMEOUT_SECONDS)
				except (OSError, SystemError, AttributeError):
					pass
		if self._process is not None and not self._cleanup_complete:
			cleanup_started = time.monotonic()
			try:
				self._cleanup_worker_tree()
			except BrowserWorkerError as exc:
				cleanup_error = exc
			except Exception:
				_diagnostic(
					self._data_dir,
					stage="shutdown",
					operation="cleanup",
					started=cleanup_started,
					error_code="BROWSER_ERROR",
				)
				try:
					self._cleanup_worker_tree()
				except BrowserWorkerError as exc:
					cleanup_error = exc
				except Exception:
					cleanup_error = BrowserWorkerError("BROWSER_PROFILE_NOT_CLEAN", "cleanup")
		self._close_connection()
		if cleanup_error is not None:
			raise cleanup_error

	def __enter__(self) -> "SafeBrowserWorker":
		return self

	def __exit__(self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: Any) -> None:
		self.close()


class SafeBrowserPlatform:
	"""父进程中的最小平台代理，只暴露安全入口实际需要的调用。"""

	def __init__(self, data_dir: Path, *, delay: tuple[float, float] = (2.0, 5.0)) -> None:
		self._worker = SafeBrowserWorker(data_dir, delay=delay)

	def close(self) -> None:
		self._worker.close()

	def __enter__(self) -> "SafeBrowserPlatform":
		return self

	def __exit__(self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: Any) -> None:
		self.close()

	def search_jobs(self, query: str, **filters: Any) -> dict[str, Any]:
		return self._worker.call("search_jobs", query=query, filters=filters)

	def user_info(self) -> dict[str, Any]:
		return self._worker.call("user_info")

	def greet(self, security_id: str, job_id: str, message: str = "") -> dict[str, Any]:
		return self._worker.call("greet", security_id=security_id, job_id=job_id, message=message)

	def is_success(self, response: dict[str, Any]) -> bool:
		return response.get("code") == 0

	def unwrap_data(self, response: dict[str, Any]) -> Any:
		return response.get("zpData")

	def parse_error(self, response: dict[str, Any]) -> tuple[str, str]:
		from boss_agent_cli.platforms.zhipin import BossPlatform

		return BossPlatform(cast(Any, None)).parse_error(response)

	def apply(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
		raise RuntimeError("安全入口不得调用 apply")


__all__ = [
	"BROWSER_CALL_TIMEOUT_SECONDS",
	"BROWSER_INIT_TIMEOUT_SECONDS",
	"BrowserWorkerError",
	"record_diagnostic",
	"run_login_in_worker",
	"SafeBrowserPlatform",
	"SafeBrowserWorker",
]
