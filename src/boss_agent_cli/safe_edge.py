"""boss-safe 专用可见 Chromium 浏览器的严格 CDP 运行时。

运行时只会启动或复用登记到 boss-safe 独立 profile 的浏览器。PID、创建时间、
可执行文件、命令行 profile、进程树和 loopback 端口必须同时吻合；关闭时也只
终止这棵已验证的树，绝不按进程名清理，更不会删除 profile。
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, cast
from urllib.parse import urlparse

import psutil

from boss_agent_cli.safe_config import BrowserConfig


BROWSER_CDP_HOST = "127.0.0.1"
BROWSER_CDP_PORT = 9222
# 为兼容已存在的 Windows 安全 profile，磁盘目录名保持不变；所有公共输出使用
# “专用浏览器”这一通用称呼。
BROWSER_PROFILE_DIRNAME = "edge-cdp-profile"
BROWSER_RUNTIME_FILENAME = "edge-cdp-runtime.json"
BROWSER_HOME_URL = "https://www.zhipin.com/"
BROWSER_START_TIMEOUT_SECONDS = 30.0

# 旧安全入口和第三方脚本可能导入这些常量，保留为兼容别名。
EDGE_CDP_HOST = BROWSER_CDP_HOST
EDGE_CDP_PORT = BROWSER_CDP_PORT
EDGE_PROFILE_DIRNAME = BROWSER_PROFILE_DIRNAME
EDGE_RUNTIME_FILENAME = BROWSER_RUNTIME_FILENAME
EDGE_HOME_URL = BROWSER_HOME_URL
EDGE_START_TIMEOUT_SECONDS = BROWSER_START_TIMEOUT_SECONDS


class BrowserRuntimeError(RuntimeError):
	"""专用浏览器生命周期的稳定、可供安全门禁识别的错误。"""

	def __init__(self, code: str, stage: str, message: str) -> None:
		self.code = code
		self.stage = stage
		super().__init__(message)


EdgeRuntimeError = BrowserRuntimeError


@dataclass(frozen=True, slots=True)
class BrowserExecutable:
	kind: str
	path: Path


@dataclass(frozen=True, slots=True)
class BrowserProcessInfo:
	pid: int
	parent_pid: int
	executable_path: str
	command_line: str
	creation_time: str


EdgeProcessInfo = BrowserProcessInfo


@dataclass(frozen=True, slots=True)
class BrowserRuntime:
	pid: int
	creation_time: str
	port: int
	profile: str
	executable: str
	started_at: float
	browser_kind: str = "edge"


EdgeRuntime = BrowserRuntime


def browser_profile_path(data_dir: Path) -> Path:
	return data_dir / BROWSER_PROFILE_DIRNAME


def edge_profile_path(data_dir: Path) -> Path:
	return browser_profile_path(data_dir)


def browser_runtime_path(data_dir: Path) -> Path:
	return data_dir / BROWSER_RUNTIME_FILENAME


def edge_runtime_path(data_dir: Path) -> Path:
	return browser_runtime_path(data_dir)


def browser_cdp_url(port: int = BROWSER_CDP_PORT) -> str:
	return f"http://{BROWSER_CDP_HOST}:{port}"


def edge_cdp_url(port: int = BROWSER_CDP_PORT) -> str:
	return browser_cdp_url(port)


def _normal_path(value: str | Path) -> str:
	try:
		text = str(Path(value).expanduser().resolve(strict=False))
	except (OSError, RuntimeError, ValueError):
		text = str(value)
	return os.path.normcase(os.path.normpath(text))


def _command_contains_path(command_line: str, path: str | Path) -> bool:
	needle = _normal_path(path)
	command = command_line.replace('"', "")
	if os.name == "nt":
		command = command.replace("/", "\\").casefold()
		needle = needle.replace("/", "\\").casefold()
	return bool(needle) and needle in command


def _creation_token(value: float | str) -> str:
	if isinstance(value, str):
		return value
	return f"{value:.6f}"


def _kind_from_name(path: Path) -> str:
	name = path.name.casefold()
	if "edge" in name:
		return "edge"
	if "chromium" in name:
		return "chromium"
	return "chrome"


def _well_known_candidates(kind: str, system: str | None = None) -> list[Path]:
	system = system or platform.system()
	home = Path.home()
	items: list[Path] = []
	if system == "Windows":
		program_files = [os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")]
		local = os.environ.get("LOCALAPPDATA")
		bases = [Path(value) for value in program_files if value]
		if local:
			bases.append(Path(local))
		if kind == "edge":
			items.extend(base / "Microsoft" / "Edge" / "Application" / "msedge.exe" for base in bases)
		elif kind == "chrome":
			items.extend(base / "Google" / "Chrome" / "Application" / "chrome.exe" for base in bases)
		elif kind == "chromium":
			items.extend(base / "Chromium" / "Application" / "chrome.exe" for base in bases)
	elif system == "Darwin":
		applications = (Path("/Applications"), home / "Applications")
		bundle = {
			"edge": "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
			"chrome": "Google Chrome.app/Contents/MacOS/Google Chrome",
			"chromium": "Chromium.app/Contents/MacOS/Chromium",
		}[kind]
		items.extend(base / bundle for base in applications)
	else:
		for value in {
			"edge": ("microsoft-edge", "microsoft-edge-stable"),
			"chrome": ("google-chrome", "google-chrome-stable"),
			"chromium": ("chromium", "chromium-browser"),
		}[kind]:
			resolved = shutil.which(value)
			if resolved:
				items.append(Path(resolved))
	return items


def find_browser_executable(config: BrowserConfig | None = None) -> BrowserExecutable | None:
	"""按 Edge、Chrome、Chromium 顺序发现浏览器，或使用显式可执行文件。"""
	config = config or BrowserConfig()
	config.validate()
	if config.executable:
		candidate = Path(config.executable).expanduser()
		if not candidate.is_file():
			return None
		kind = config.kind if config.kind != "auto" else _kind_from_name(candidate)
		return BrowserExecutable(kind, candidate.resolve())
	kinds = ("edge", "chrome", "chromium") if config.kind == "auto" else (config.kind,)
	for kind in kinds:
		seen: set[str] = set()
		for candidate in _well_known_candidates(kind):
			key = _normal_path(candidate)
			if key in seen:
				continue
			seen.add(key)
			if candidate.is_file():
				return BrowserExecutable(kind, candidate.resolve())
	return None


def find_edge_executable() -> Path | None:
	"""兼容旧调用：只查找 Edge。新代码应使用 ``find_browser_executable``。"""
	found = find_browser_executable(BrowserConfig(kind="edge"))
	return found.path if found is not None else None


def process_snapshot() -> list[BrowserProcessInfo]:
	"""通过 psutil 获取跨平台进程快照；无法读取的进程保留 PID 并留空敏感字段。"""
	items: list[BrowserProcessInfo] = []
	for process in psutil.process_iter(["pid", "ppid", "exe", "cmdline", "create_time"], ad_value=None):
		try:
			info = process.info
			cmdline = info.get("cmdline") or []
			items.append(BrowserProcessInfo(
				pid=int(info["pid"]),
				parent_pid=int(info.get("ppid") or 0),
				executable_path=str(info.get("exe") or ""),
				command_line=" ".join(map(str, cmdline)) if isinstance(cmdline, (list, tuple)) else str(cmdline),
				creation_time=_creation_token(float(info["create_time"])) if info.get("create_time") else "",
			))
		except (psutil.NoSuchProcess, psutil.ZombieProcess, OSError, TypeError, ValueError):
			continue
	return items


def windows_process_snapshot() -> list[BrowserProcessInfo]:
	"""兼容旧测试和调用的跨平台快照别名。"""
	return process_snapshot()


def _pid_exists(pid: int) -> bool:
	return pid > 0 and psutil.pid_exists(pid)


def _descendant_pids(root_pid: int, snapshot: Iterable[BrowserProcessInfo]) -> set[int]:
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


def _profile_processes(profile: Path, snapshot: Iterable[BrowserProcessInfo]) -> list[BrowserProcessInfo]:
	return [item for item in snapshot if _command_contains_path(item.command_line, profile)]


def _profile_locks(profile: Path) -> list[str]:
	return [name for name in ("SingletonLock", "SingletonCookie", "SingletonSocket") if (profile / name).exists()]


def _read_runtime(data_dir: Path) -> BrowserRuntime | None:
	try:
		decoded: Any = json.loads(browser_runtime_path(data_dir).read_text(encoding="utf-8"))
		if not isinstance(decoded, dict):
			return None
		return BrowserRuntime(
			pid=int(decoded["pid"]),
			creation_time=str(decoded["creation_time"]),
			port=int(decoded["port"]),
			profile=str(decoded["profile"]),
			executable=str(decoded["executable"]),
			started_at=float(decoded["started_at"]),
			browser_kind=str(decoded.get("browser_kind") or _kind_from_name(Path(str(decoded["executable"])))),
		)
	except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
		return None


def _write_runtime(data_dir: Path, runtime: BrowserRuntime) -> None:
	data_dir.mkdir(parents=True, exist_ok=True)
	path = browser_runtime_path(data_dir)
	temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
	try:
		temporary.write_text(
			json.dumps(asdict(runtime), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
			encoding="utf-8",
		)
		if os.name != "nt":
			os.chmod(temporary, 0o600)
		os.replace(temporary, path)
	finally:
		temporary.unlink(missing_ok=True)


def _runtime_process(runtime: BrowserRuntime, snapshot: Iterable[BrowserProcessInfo]) -> BrowserProcessInfo | None:
	for process in snapshot:
		if process.pid != runtime.pid or process.creation_time != runtime.creation_time:
			continue
		if _normal_path(process.executable_path) != _normal_path(runtime.executable):
			return None
		if not _command_contains_path(process.command_line, runtime.profile):
			return None
		return process
	return None


def _port_owner_pids(port: int) -> set[int]:
	try:
		connections = psutil.net_connections(kind="tcp")
	except (psutil.AccessDenied, OSError) as exc:
		raise BrowserRuntimeError("BROWSER_ERROR", "browser_port", "权限不足，无法验证本地 CDP 端口归属") from exc
	owners: set[int] = set()
	for connection in connections:
		if connection.status != psutil.CONN_LISTEN or not connection.laddr:
			continue
		address = str(connection.laddr.ip)
		local_port = int(connection.laddr.port)
		if local_port == port and address in {BROWSER_CDP_HOST, "::1"} and connection.pid:
			owners.add(int(connection.pid))
	return owners


def _port_in_use(port: int) -> bool:
	try:
		with socket.create_connection((BROWSER_CDP_HOST, port), timeout=0.3):
			return True
	except OSError:
		return False


def _cdp_payload(port: int) -> dict[str, Any] | None:
	try:
		request = urllib.request.Request(f"{browser_cdp_url(port)}/json/version", method="GET")
		with urllib.request.urlopen(request, timeout=1.5) as response:
			decoded: Any = json.loads(response.read().decode("utf-8"))
		if not isinstance(decoded, dict):
			return None
		parsed = urlparse(str(decoded.get("webSocketDebuggerUrl") or ""))
		if parsed.scheme not in {"ws", "wss"} or parsed.hostname not in {BROWSER_CDP_HOST, "localhost", "::1"}:
			return None
		if parsed.port != port:
			return None
		return cast("dict[str, Any]", decoded)
	except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError, urllib.error.URLError):
		return None


def _runtime_is_owned(runtime: BrowserRuntime, snapshot: list[BrowserProcessInfo]) -> bool:
	root = _runtime_process(runtime, snapshot)
	if root is None:
		return False
	owners = _port_owner_pids(runtime.port)
	return bool(owners) and owners.issubset(_descendant_pids(root.pid, snapshot))


def browser_runtime_status(data_dir: Path, config: BrowserConfig | None = None) -> dict[str, Any]:
	runtime = _read_runtime(data_dir)
	runtime_invalid = browser_runtime_path(data_dir).exists() and runtime is None
	profile = Path(runtime.profile) if runtime is not None else browser_profile_path(data_dir)
	port = runtime.port if runtime is not None else (config.cdp_port if config is not None else BROWSER_CDP_PORT)
	inspection_error: str | None = None
	try:
		snapshot = process_snapshot()
		profile_processes = _profile_processes(profile, snapshot)
		owned = runtime is not None and _runtime_is_owned(runtime, snapshot)
	except BrowserRuntimeError as exc:
		profile_processes = []
		owned = False
		inspection_error = exc.code
	cdp_ready = runtime is not None and owned and _cdp_payload(runtime.port) is not None
	locks = _profile_locks(profile)
	runtime_pid_exists = runtime is not None and _pid_exists(runtime.pid)
	return {
		"mode": "strict_cdp",
		"browser_kind": runtime.browser_kind if runtime is not None else (config.kind if config is not None else "auto"),
		"profile": str(profile),
		"host": BROWSER_CDP_HOST,
		"port": port,
		"running": bool(profile_processes or runtime_pid_exists or locks),
		"owned": owned,
		"cdp_ready": cdp_ready,
		"pid": runtime.pid if owned and runtime is not None else None,
		"profile_process_count": len(profile_processes),
		"locks": locks,
		"inspection_error": inspection_error,
		"runtime_registered": runtime is not None,
		"runtime_invalid": runtime_invalid,
	}


def _adopt_owned_browser(
	data_dir: Path,
	browser: BrowserExecutable,
	port: int,
	snapshot: list[BrowserProcessInfo],
) -> BrowserRuntime | None:
	profile = browser_profile_path(data_dir)
	owners = _port_owner_pids(port)
	if not owners or _cdp_payload(port) is None:
		return None
	candidates = [
		item
		for item in _profile_processes(profile, snapshot)
		if item.creation_time
		and _normal_path(item.executable_path) == _normal_path(browser.path)
		and owners.issubset(_descendant_pids(item.pid, snapshot))
	]
	if not candidates:
		return None
	root = max(candidates, key=lambda item: len(_descendant_pids(item.pid, snapshot)))
	runtime = BrowserRuntime(
		pid=root.pid,
		creation_time=root.creation_time,
		port=port,
		profile=str(profile.resolve()),
		executable=str(browser.path.resolve()),
		started_at=time.time(),
		browser_kind=browser.kind,
	)
	_write_runtime(data_dir, runtime)
	return runtime


def _terminate_process_tree(runtime: BrowserRuntime, snapshot: list[BrowserProcessInfo]) -> None:
	root = _runtime_process(runtime, snapshot)
	if root is None:
		raise BrowserRuntimeError("BROWSER_PROFILE_NOT_CLEAN", "browser_shutdown", "专用浏览器 PID 或创建时间已变化")
	known_pids = _descendant_pids(root.pid, snapshot)
	by_pid = {item.pid: item for item in snapshot if item.pid in known_pids}
	processes: list[psutil.Process] = []
	try:
		for pid in known_pids:
			process = psutil.Process(pid)
			expected = by_pid.get(pid)
			if expected is None or _creation_token(process.create_time()) != expected.creation_time:
				raise BrowserRuntimeError("BROWSER_PROFILE_NOT_CLEAN", "browser_shutdown", "检测到 PID 复用，拒绝终止进程")
			processes.append(process)
		for process in sorted(processes, key=lambda item: item.pid == root.pid):
			process.terminate()
		_, alive = psutil.wait_procs(processes, timeout=5)
		for process in alive:
			process.kill()
		_, alive = psutil.wait_procs(alive, timeout=3)
		if alive:
			raise BrowserRuntimeError("BROWSER_PROFILE_NOT_CLEAN", "browser_shutdown", "专用浏览器进程树未能完整关闭")
	except BrowserRuntimeError:
		raise
	except psutil.NoSuchProcess:
		return
	except (psutil.AccessDenied, OSError) as exc:
		raise BrowserRuntimeError("BROWSER_PROFILE_NOT_CLEAN", "browser_shutdown", "权限不足，无法精确关闭专用浏览器") from exc


def ensure_browser_running(
	data_dir: Path,
	config: BrowserConfig | None = None,
	*,
	timeout: float = BROWSER_START_TIMEOUT_SECONDS,
) -> str:
	"""启动或精确复用专用可见浏览器，返回严格 loopback CDP URL。"""
	config = config or BrowserConfig()
	config.validate()
	browser = find_browser_executable(config)
	if browser is None:
		raise BrowserRuntimeError("BROWSER_ERROR", "browser_launch", "未找到配置允许的 Edge、Chrome 或 Chromium")
	profile = browser_profile_path(data_dir)
	profile.mkdir(parents=True, exist_ok=True)
	snapshot = process_snapshot()
	runtime = _read_runtime(data_dir)
	if browser_runtime_path(data_dir).exists() and runtime is None:
		raise BrowserRuntimeError("BROWSER_PROFILE_NOT_CLEAN", "browser_launch", "专用浏览器运行元数据损坏，拒绝启动")
	if runtime is not None and _runtime_is_owned(runtime, snapshot) and _cdp_payload(runtime.port) is not None:
		if (
			runtime.port != config.cdp_port
			or _normal_path(runtime.executable) != _normal_path(browser.path)
			or _normal_path(runtime.profile) != _normal_path(profile)
		):
			raise BrowserRuntimeError("BROWSER_PROFILE_NOT_CLEAN", "browser_launch", "运行中的专用浏览器与当前配置不一致")
		return browser_cdp_url(runtime.port)

	if _port_in_use(config.cdp_port):
		adopted = _adopt_owned_browser(data_dir, browser, config.cdp_port, snapshot)
		if adopted is not None:
			return browser_cdp_url(adopted.port)
		raise BrowserRuntimeError("BROWSER_ERROR", "browser_launch", "本地 CDP 端口已被非本任务进程占用")
	if _profile_processes(profile, snapshot) or _profile_locks(profile):
		raise BrowserRuntimeError(
			"BROWSER_PROFILE_NOT_CLEAN",
			"browser_launch",
			"专用浏览器 profile 已被未登记进程占用，请先人工关闭该窗口",
		)

	args = [
		str(browser.path),
		f"--remote-debugging-address={BROWSER_CDP_HOST}",
		f"--remote-debugging-port={config.cdp_port}",
		f"--user-data-dir={profile.resolve()}",
		"--no-first-run",
		"--no-default-browser-check",
		BROWSER_HOME_URL,
	]
	kwargs: dict[str, Any] = {
		"stdin": subprocess.DEVNULL,
		"stdout": subprocess.DEVNULL,
		"stderr": subprocess.DEVNULL,
	}
	if os.name == "nt":
		kwargs["creationflags"] = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
	else:
		kwargs["start_new_session"] = True
	try:
		process = subprocess.Popen(args, **kwargs)
	except OSError as exc:
		raise BrowserRuntimeError("BROWSER_ERROR", "browser_launch", "专用浏览器启动失败") from exc

	deadline = time.monotonic() + timeout
	while time.monotonic() < deadline:
		if _cdp_payload(config.cdp_port) is not None:
			snapshot = process_snapshot()
			adopted = _adopt_owned_browser(data_dir, browser, config.cdp_port, snapshot)
			if adopted is not None:
				return browser_cdp_url(adopted.port)
		if process.poll() is not None and not _port_in_use(config.cdp_port):
			break
		time.sleep(0.2)

	after = process_snapshot()
	profile_items = _profile_processes(profile, after)
	if profile_items:
		root = max(profile_items, key=lambda item: len(_descendant_pids(item.pid, after)))
		provisional = BrowserRuntime(
			pid=root.pid,
			creation_time=root.creation_time,
			port=config.cdp_port,
			profile=str(profile.resolve()),
			executable=str(browser.path.resolve()),
			started_at=time.time(),
			browser_kind=browser.kind,
		)
		_terminate_process_tree(provisional, after)
	else:
		try:
			process.terminate()
			process.wait(timeout=3)
		except (OSError, subprocess.SubprocessError):
			pass
	if _profile_processes(profile, process_snapshot()):
		raise BrowserRuntimeError("BROWSER_PROFILE_NOT_CLEAN", "browser_launch", "浏览器启动超时且进程未能完整回收")
	raise BrowserRuntimeError("BROWSER_HUNG", "browser_launch", "专用浏览器未在限定时间内开放本地 CDP")


def ensure_edge_running(
	data_dir: Path,
	config: BrowserConfig | None = None,
	*,
	timeout: float = BROWSER_START_TIMEOUT_SECONDS,
) -> str:
	return ensure_browser_running(data_dir, config, timeout=timeout)


def strict_cdp_url(data_dir: Path) -> str:
	"""只读取并验证已登记浏览器；worker 不负责启动或切换浏览器。"""
	runtime = _read_runtime(data_dir)
	if runtime is None:
		raise BrowserRuntimeError("BROWSER_PAGE_NOT_READY", "cdp_attach", "专用浏览器未运行，请先执行 boss-safe login")
	snapshot = process_snapshot()
	if not _runtime_is_owned(runtime, snapshot) or _cdp_payload(runtime.port) is None:
		raise BrowserRuntimeError("BROWSER_PAGE_NOT_READY", "cdp_attach", "专用浏览器 CDP 不可用，请重新执行 boss-safe login")
	return browser_cdp_url(runtime.port)


def strict_cdp_user_agent(data_dir: Path) -> str:
	"""从已验证的本地 CDP 元数据读取 UA，不进入页面执行脚本。"""
	runtime = _read_runtime(data_dir)
	if runtime is None:
		raise BrowserRuntimeError("BROWSER_PAGE_NOT_READY", "cdp_attach", "专用浏览器未运行，请先执行 boss-safe login")
	snapshot = process_snapshot()
	payload = _cdp_payload(runtime.port)
	if not _runtime_is_owned(runtime, snapshot) or payload is None:
		raise BrowserRuntimeError("BROWSER_PAGE_NOT_READY", "cdp_attach", "专用浏览器 CDP 不可用，请重新执行 boss-safe login")
	user_agent = str(payload.get("User-Agent") or "").strip()
	if not user_agent:
		raise BrowserRuntimeError("BROWSER_ERROR", "cdp_attach", "专用浏览器 CDP 未提供 User-Agent")
	return user_agent


def stop_browser(data_dir: Path) -> dict[str, Any]:
	"""精确关闭登记的专用浏览器 PID 树，保留全部 profile 数据。"""
	runtime = _read_runtime(data_dir)
	if browser_runtime_path(data_dir).exists() and runtime is None:
		raise BrowserRuntimeError("BROWSER_PROFILE_NOT_CLEAN", "browser_shutdown", "专用浏览器运行元数据损坏，拒绝模糊清理")
	profile = Path(runtime.profile) if runtime is not None else browser_profile_path(data_dir)
	snapshot = process_snapshot()
	profile_processes = _profile_processes(profile, snapshot)
	if runtime is None:
		if profile_processes or _profile_locks(profile):
			raise BrowserRuntimeError(
				"BROWSER_PROFILE_NOT_CLEAN",
				"browser_shutdown",
				"检测到未登记的专用 profile 进程，拒绝按名称或模糊条件关闭",
			)
		return {"stopped": False, "profile": str(profile), "remaining_processes": 0}
	root = _runtime_process(runtime, snapshot)
	if root is None:
		if _pid_exists(runtime.pid) or profile_processes:
			raise BrowserRuntimeError(
				"BROWSER_PROFILE_NOT_CLEAN",
				"browser_shutdown",
				"运行时 PID 已变化或权限不足，拒绝结束归属不明的浏览器",
			)
		try:
			browser_runtime_path(data_dir).unlink(missing_ok=True)
		except OSError as exc:
			raise BrowserRuntimeError("BROWSER_PROFILE_NOT_CLEAN", "browser_shutdown", "无法清理失效运行时元数据") from exc
		return {"stopped": False, "profile": str(profile), "remaining_processes": 0}

	_terminate_process_tree(runtime, snapshot)
	deadline = time.monotonic() + 3
	remaining: list[BrowserProcessInfo] = []
	locks = _profile_locks(profile)
	while time.monotonic() < deadline:
		remaining = _profile_processes(profile, process_snapshot())
		locks = _profile_locks(profile)
		if not remaining and not locks:
			break
		time.sleep(0.1)
	if remaining or locks:
		raise BrowserRuntimeError("BROWSER_PROFILE_NOT_CLEAN", "browser_shutdown", "专用浏览器进程树未能完整关闭")
	try:
		browser_runtime_path(data_dir).unlink(missing_ok=True)
	except OSError as exc:
		raise BrowserRuntimeError("BROWSER_PROFILE_NOT_CLEAN", "browser_shutdown", "无法清理浏览器运行时元数据") from exc
	return {
		"stopped": True,
		"browser_kind": runtime.browser_kind,
		"profile": str(profile),
		"remaining_processes": 0,
		"port_in_use": _port_in_use(runtime.port),
	}


def stop_edge(data_dir: Path) -> dict[str, Any]:
	return stop_browser(data_dir)


__all__ = [
	"BROWSER_CDP_HOST", "BROWSER_CDP_PORT", "BROWSER_PROFILE_DIRNAME", "BrowserExecutable",
	"BrowserProcessInfo", "BrowserRuntime", "BrowserRuntimeError", "EDGE_CDP_HOST", "EDGE_CDP_PORT",
	"EDGE_PROFILE_DIRNAME", "EdgeProcessInfo", "EdgeRuntime", "EdgeRuntimeError", "browser_cdp_url",
	"browser_profile_path", "browser_runtime_path", "browser_runtime_status", "edge_cdp_url", "edge_profile_path",
	"edge_runtime_path", "ensure_browser_running", "ensure_edge_running", "find_browser_executable",
	"find_edge_executable", "process_snapshot", "stop_browser", "stop_edge", "strict_cdp_user_agent",
	"strict_cdp_url", "windows_process_snapshot",
]
