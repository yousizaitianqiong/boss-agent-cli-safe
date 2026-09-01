"""在全新虚拟环境中安装 wheel 并执行离线 CLI 冒烟。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path


def _script_path(environment: Path, name: str) -> Path:
	if os.name == "nt":
		return environment / "Scripts" / f"{name}.exe"
	return environment / "bin" / name


def _run(args: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
	result = subprocess.run(
		args,
		cwd=cwd,
		env=env,
		text=True,
		encoding="utf-8",
		errors="replace",
		capture_output=True,
		check=False,
		timeout=300,
	)
	if result.returncode:
		raise RuntimeError(
			"command failed: "
			+ " ".join(args)
			+ f"\nstdout:\n{result.stdout[-4000:]}\nstderr:\n{result.stderr[-4000:]}"
		)
	return result


def main() -> int:
	dist_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "dist").resolve()
	wheels = sorted(dist_dir.glob("boss_agent_cli_safe-*.whl"))
	if len(wheels) != 1:
		raise SystemExit(f"预期恰好一个安全版 wheel，实际为 {len(wheels)}")
	with tempfile.TemporaryDirectory(prefix="boss-safe-wheel-") as temporary:
		root = Path(temporary)
		environment = root / "venv"
		venv.EnvBuilder(with_pip=True, clear=True).create(environment)
		python = _script_path(environment, "python")
		_run([str(python), "-m", "pip", "install", str(wheels[0])], cwd=root, env=os.environ.copy())
		clean_env = os.environ.copy()
		clean_env.pop("PYTHONPATH", None)
		boss_safe = _script_path(environment, "boss-safe")
		boss = _script_path(environment, "boss")
		boss_mcp = _script_path(environment, "boss-mcp")
		for entrypoint in (boss_safe, boss, boss_mcp):
			if not entrypoint.is_file():
				raise SystemExit(f"缺少入口：{entrypoint.name}")
		_run([str(boss_safe), "--help"], cwd=root, env=clean_env)
		_run([str(boss), "--version"], cwd=root, env=clean_env)
		state_dir = root / "state"
		status = _run([str(boss_safe), "--data-dir", str(state_dir), "status"], cwd=root, env=clean_env)
		payload = json.loads(status.stdout)
		if payload.get("data", {}).get("config_state") != "missing":
			raise SystemExit("未初始化状态冒烟结果不符合预期")
		_run([str(boss_safe), "--data-dir", str(state_dir), "browser-stop"], cwd=root, env=clean_env)
	print(f"wheel smoke ok: {wheels[0].name}")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
