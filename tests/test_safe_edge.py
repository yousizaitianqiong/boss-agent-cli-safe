"""跨平台专用浏览器严格 CDP 生命周期的离线契约测试。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import boss_agent_cli.safe_edge as edge_module
from boss_agent_cli.safe_config import BrowserConfig
from boss_agent_cli.safe_edge import BrowserExecutable, EdgeProcessInfo, EdgeRuntime, EdgeRuntimeError


def _runtime(tmp_path: Path, executable: Path, *, creation_time: str = "created-1") -> EdgeRuntime:
	profile = edge_module.edge_profile_path(tmp_path)
	profile.mkdir(parents=True, exist_ok=True)
	runtime = EdgeRuntime(
		pid=100,
		creation_time=creation_time,
		port=edge_module.EDGE_CDP_PORT,
		profile=str(profile.resolve()),
		executable=str(executable.resolve()),
		started_at=123.0,
	)
	edge_module._write_runtime(tmp_path, runtime)  # noqa: SLF001 - 离线构造精确运行时
	return runtime


def _root_process(tmp_path: Path, executable: Path, *, creation_time: str = "created-1") -> EdgeProcessInfo:
	profile = edge_module.edge_profile_path(tmp_path).resolve()
	return EdgeProcessInfo(
		pid=100,
		parent_pid=1,
		executable_path=str(executable.resolve()),
		command_line=f'"{executable}" --user-data-dir="{profile}" --remote-debugging-port=9222',
		creation_time=creation_time,
	)


def test_runtime_status_requires_pid_creation_profile_and_loopback_cdp(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	executable = tmp_path / "msedge.exe"
	executable.write_bytes(b"edge")
	_runtime(tmp_path, executable)
	root = _root_process(tmp_path, executable)
	monkeypatch.setattr(edge_module, "process_snapshot", lambda: [root])
	monkeypatch.setattr(edge_module, "_port_owner_pids", lambda port: {100})
	monkeypatch.setattr(edge_module, "_cdp_payload", lambda port: {"webSocketDebuggerUrl": "ws://127.0.0.1:9222/x"})

	status = edge_module.browser_runtime_status(tmp_path)

	assert status["mode"] == "strict_cdp"
	assert status["running"] is True
	assert status["owned"] is True
	assert status["cdp_ready"] is True
	assert status["pid"] == 100
	assert status["profile_process_count"] == 1


def test_runtime_metadata_contains_no_browser_credentials(tmp_path: Path) -> None:
	executable = tmp_path / "msedge.exe"
	executable.write_bytes(b"edge")
	_runtime(tmp_path, executable)

	payload = json.loads(edge_module.edge_runtime_path(tmp_path).read_text(encoding="utf-8"))

	assert set(payload) == {"pid", "creation_time", "port", "profile", "executable", "started_at", "browser_kind"}
	assert not {"cookie", "cookies", "token", "stoken", "security_id"} & set(payload)


def test_ensure_edge_reuses_only_exact_owned_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	executable = tmp_path / "msedge.exe"
	executable.write_bytes(b"edge")
	_runtime(tmp_path, executable)
	root = _root_process(tmp_path, executable)
	monkeypatch.setattr(edge_module, "find_browser_executable", lambda config=None: BrowserExecutable("edge", executable))
	monkeypatch.setattr(edge_module, "process_snapshot", lambda: [root])
	monkeypatch.setattr(edge_module, "_port_owner_pids", lambda port: {100})
	monkeypatch.setattr(edge_module, "_cdp_payload", lambda port: {"webSocketDebuggerUrl": "ws://127.0.0.1:9222/x"})
	monkeypatch.setattr(edge_module.subprocess, "Popen", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no launch")))

	assert edge_module.ensure_edge_running(tmp_path) == "http://127.0.0.1:9222"


def test_strict_cdp_user_agent_comes_from_owned_local_runtime(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	executable = tmp_path / "msedge.exe"
	executable.write_bytes(b"edge")
	_runtime(tmp_path, executable)
	root = _root_process(tmp_path, executable)
	monkeypatch.setattr(edge_module, "process_snapshot", lambda: [root])
	monkeypatch.setattr(edge_module, "_port_owner_pids", lambda port: {100})
	monkeypatch.setattr(
		edge_module,
		"_cdp_payload",
		lambda port: {
			"webSocketDebuggerUrl": "ws://127.0.0.1:9222/x",
			"User-Agent": "Exact Edge CDP UA",
		},
	)

	assert edge_module.strict_cdp_user_agent(tmp_path) == "Exact Edge CDP UA"


def test_unknown_port_owner_is_refused_without_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	executable = tmp_path / "msedge.exe"
	executable.write_bytes(b"edge")
	monkeypatch.setattr(edge_module, "find_browser_executable", lambda config=None: BrowserExecutable("edge", executable))
	monkeypatch.setattr(edge_module, "process_snapshot", lambda: [])
	monkeypatch.setattr(edge_module, "_port_in_use", lambda port: True)
	monkeypatch.setattr(edge_module, "_adopt_owned_browser", lambda *args: None)
	monkeypatch.setattr(edge_module.subprocess, "Popen", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no launch")))

	with pytest.raises(EdgeRuntimeError) as error:
		edge_module.ensure_edge_running(tmp_path)

	assert error.value.code == "BROWSER_ERROR"
	assert error.value.stage == "browser_launch"


class _FakePopen:
	pid = 100

	def poll(self) -> int | None:
		return None

	def wait(self, timeout: float | None = None) -> int:
		return 0


def test_edge_launch_uses_visible_dedicated_profile_and_loopback_cdp(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	executable = tmp_path / "msedge.exe"
	executable.write_bytes(b"edge")
	root = _root_process(tmp_path, executable)
	launches: list[tuple[list[str], dict[str, Any]]] = []

	def fake_popen(args: list[str], **kwargs: Any) -> _FakePopen:
		launches.append((args, kwargs))
		return _FakePopen()

	snapshots = iter([[], [root]])
	monkeypatch.setattr(edge_module, "find_browser_executable", lambda config=None: BrowserExecutable("edge", executable))
	monkeypatch.setattr(edge_module, "process_snapshot", lambda: next(snapshots, [root]))
	monkeypatch.setattr(edge_module, "_port_in_use", lambda port: False)
	monkeypatch.setattr(edge_module, "_port_owner_pids", lambda port: {100})
	monkeypatch.setattr(edge_module, "_cdp_payload", lambda port: {"webSocketDebuggerUrl": "ws://127.0.0.1:9222/x"})
	monkeypatch.setattr(edge_module.subprocess, "Popen", fake_popen)

	url = edge_module.ensure_edge_running(tmp_path, timeout=0.1)

	assert url == "http://127.0.0.1:9222"
	assert len(launches) == 1
	args, kwargs = launches[0]
	assert f"--remote-debugging-address={edge_module.EDGE_CDP_HOST}" in args
	assert f"--remote-debugging-port={edge_module.EDGE_CDP_PORT}" in args
	assert f"--user-data-dir={edge_module.edge_profile_path(tmp_path).resolve()}" in args
	assert "--headless" not in args
	assert kwargs["stdin"] is edge_module.subprocess.DEVNULL


def test_browser_stop_kills_only_verified_root_tree_and_preserves_profile(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	executable = tmp_path / "msedge.exe"
	executable.write_bytes(b"edge")
	_runtime(tmp_path, executable)
	profile = edge_module.edge_profile_path(tmp_path)
	marker = profile / "keep-me.txt"
	marker.write_text("profile-data", encoding="utf-8")
	root = _root_process(tmp_path, executable)
	child = EdgeProcessInfo(101, 100, str(executable), "msedge --type=renderer", "created-2")
	snapshots = iter([[root, child], []])
	kills: list[tuple[int, bool]] = []
	monkeypatch.setattr(edge_module, "process_snapshot", lambda: next(snapshots, []))
	monkeypatch.setattr(edge_module, "_terminate_process_tree", lambda runtime, snapshot: kills.append((runtime.pid, True)))
	monkeypatch.setattr(edge_module, "_port_in_use", lambda port: False)

	result = edge_module.stop_edge(tmp_path)

	assert result["stopped"] is True
	assert kills == [(100, True)]
	assert marker.read_text(encoding="utf-8") == "profile-data"
	assert edge_module.edge_runtime_path(tmp_path).exists() is False


def test_browser_stop_refuses_reused_pid_and_does_not_kill(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	executable = tmp_path / "msedge.exe"
	executable.write_bytes(b"edge")
	_runtime(tmp_path, executable, creation_time="old")
	root = _root_process(tmp_path, executable, creation_time="new")
	kills: list[tuple[int, bool]] = []
	monkeypatch.setattr(edge_module, "process_snapshot", lambda: [root])
	monkeypatch.setattr(edge_module, "_pid_exists", lambda pid: True)
	monkeypatch.setattr(edge_module, "_terminate_process_tree", lambda runtime, snapshot: kills.append((runtime.pid, True)))

	with pytest.raises(EdgeRuntimeError) as error:
		edge_module.stop_edge(tmp_path)

	assert error.value.code == "BROWSER_PROFILE_NOT_CLEAN"
	assert error.value.stage == "browser_shutdown"
	assert kills == []
	assert edge_module.edge_runtime_path(tmp_path).exists() is True


def test_auto_discovery_prefers_edge_then_chrome_then_chromium(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	edge = tmp_path / "msedge"
	chrome = tmp_path / "chrome"
	chromium = tmp_path / "chromium"
	for path in (edge, chrome, chromium):
		path.write_bytes(b"browser")
	monkeypatch.setattr(
		edge_module,
		"_well_known_candidates",
		lambda kind, system=None: {"edge": [edge], "chrome": [chrome], "chromium": [chromium]}[kind],
	)

	assert edge_module.find_browser_executable(BrowserConfig()).path == edge.resolve()  # type: ignore[union-attr]
	edge.unlink()
	assert edge_module.find_browser_executable(BrowserConfig()).path == chrome.resolve()  # type: ignore[union-attr]
	chrome.unlink()
	assert edge_module.find_browser_executable(BrowserConfig()).path == chromium.resolve()  # type: ignore[union-attr]


def test_explicit_browser_override_and_custom_port_are_preserved(tmp_path: Path) -> None:
	executable = tmp_path / "custom-browser"
	executable.write_bytes(b"browser")
	config = BrowserConfig(kind="chromium", executable=str(executable), cdp_port=9333)

	found = edge_module.find_browser_executable(config)

	assert found == BrowserExecutable("chromium", executable.resolve())
	assert edge_module.browser_runtime_status(tmp_path, config)["port"] == 9333


def test_candidate_catalogs_cover_windows_macos_and_linux(monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setenv("ProgramFiles(x86)", r"C:\Program Files (x86)")
	monkeypatch.setenv("ProgramFiles", r"C:\Program Files")
	monkeypatch.setenv("LOCALAPPDATA", r"C:\safe-fixture\LocalAppData")
	monkeypatch.setattr(
		edge_module.shutil,
		"which",
		lambda name: f"/usr/bin/{name}" if name in {"microsoft-edge", "google-chrome", "chromium"} else None,
	)

	windows = edge_module._well_known_candidates("edge", "Windows")  # noqa: SLF001
	macos = edge_module._well_known_candidates("chrome", "Darwin")  # noqa: SLF001
	linux = edge_module._well_known_candidates("chromium", "Linux")  # noqa: SLF001

	assert any(path.name == "msedge.exe" for path in windows)
	assert any("Google Chrome.app" in str(path) for path in macos)
	assert Path("/usr/bin/chromium") in linux


def test_port_inspection_permission_failure_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setattr(
		edge_module.psutil,
		"net_connections",
		lambda **_: (_ for _ in ()).throw(edge_module.psutil.AccessDenied()),
	)

	with pytest.raises(EdgeRuntimeError) as error:
		edge_module._port_owner_pids(9222)  # noqa: SLF001

	assert error.value.code == "BROWSER_ERROR"
	assert error.value.stage == "browser_port"


def test_corrupt_runtime_and_orphan_profile_lock_are_fail_closed(tmp_path: Path) -> None:
	executable = tmp_path / "browser.exe"
	executable.write_bytes(b"browser")
	edge_module.browser_runtime_path(tmp_path).write_text("not-json", encoding="utf-8")
	with pytest.raises(EdgeRuntimeError, match="元数据损坏"):
		edge_module.ensure_browser_running(tmp_path, BrowserConfig(executable=str(executable)))
	with pytest.raises(EdgeRuntimeError, match="元数据损坏"):
		edge_module.stop_browser(tmp_path)

	edge_module.browser_runtime_path(tmp_path).unlink()
	profile = edge_module.browser_profile_path(tmp_path)
	profile.mkdir(parents=True, exist_ok=True)
	(profile / "SingletonLock").write_text("locked", encoding="utf-8")
	with pytest.raises(EdgeRuntimeError, match="未登记"):
		edge_module.stop_browser(tmp_path)
