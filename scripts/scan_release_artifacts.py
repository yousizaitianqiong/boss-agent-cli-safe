"""扫描 Git 提交树与发行包，阻止本机状态、凭据和绝对用户路径泄露。"""

from __future__ import annotations

import argparse
import re
import subprocess
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable


FORBIDDEN_BASENAMES = {
	"TODO.md",
	"INCIDENT.md",
	"PRD.md",
	"safe-config.json",
	"safe_apply.db",
	"edge-cdp-runtime.json",
}
SENSITIVE_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
	("Windows user directory", re.compile(rb"(?i)[a-z]:\\(?:users|documents and settings)\\[^\\\r\n]+")),
	("macOS user directory", re.compile(rb"/" + rb"Users/[^/\s]+/")),
	("Linux user directory", re.compile(rb"/" + rb"home/[^/\s]+/")),
	("local resume workspace", re.compile(rb"(?i)[a-z]:\\[^\r\n]*0(?:\\|/)[^\r\n]*")),
	(
		"possible credential assignment",
		re.compile(
			r"(?i)\b(?:stoken|token|cookie|security[_-]?id)\b\s*[:=]\s*['\"]?[A-Za-z0-9_+/=.\-]{24,}".encode()
		),
	),
	("JWT", re.compile(rb"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
)


def _member_name_is_safe(name: str) -> bool:
	path = PurePosixPath(name.replace("\\", "/"))
	if path.is_absolute() or ".." in path.parts:
		return False
	return not any(part in FORBIDDEN_BASENAMES or part == "edge-cdp-profile" for part in path.parts)


def _scan_bytes(label: str, content: bytes) -> list[str]:
	if b"\x00" in content[:4096]:
		return []
	findings: list[str] = []
	for description, pattern in SENSITIVE_PATTERNS:
		if description.endswith("user directory") and label.startswith(("git:tests/", "git:docs/")):
			# 测试与排障文档需要覆盖跨平台绝对路径解析；仍继续扫描凭据和 JWT。
			continue
		if pattern.search(content):
			findings.append(f"{label}: {description}")
	return findings


def _tracked_files(root: Path) -> Iterable[tuple[str, bytes]]:
	result = subprocess.run(
		["git", "ls-files", "-z"],
		cwd=root,
		capture_output=True,
		check=True,
	)
	for raw_name in result.stdout.split(b"\0"):
		if not raw_name:
			continue
		name = raw_name.decode("utf-8", errors="surrogateescape")
		path = root / name
		if path.is_file():
			yield name, path.read_bytes()


def _archive_files(path: Path) -> Iterable[tuple[str, bytes]]:
	if path.suffix == ".whl" or path.suffix == ".zip":
		with zipfile.ZipFile(path) as archive:
			for member in archive.infolist():
				if not member.is_dir():
					yield member.filename, archive.read(member)
	elif path.name.endswith((".tar.gz", ".tgz")):
		with tarfile.open(path, "r:gz") as archive:
			for member in archive.getmembers():
				if member.isfile():
					handle = archive.extractfile(member)
					if handle is not None:
						yield member.name, handle.read()


def _scan_entries(prefix: str, entries: Iterable[tuple[str, bytes]]) -> list[str]:
	findings: list[str] = []
	for name, content in entries:
		if not _member_name_is_safe(name):
			findings.append(f"{prefix}:{name}: forbidden filename or archive path")
		findings.extend(_scan_bytes(f"{prefix}:{name}", content))
	return findings


def main() -> int:
	parser = argparse.ArgumentParser()
	parser.add_argument("--git-tree", action="store_true", help="scan Git-tracked files")
	parser.add_argument("--artifacts", type=Path, help="scan wheel and sdist files in a directory")
	args = parser.parse_args()
	root = Path.cwd()
	findings: list[str] = []
	if args.git_tree:
		findings.extend(_scan_entries("git", _tracked_files(root)))
	if args.artifacts:
		for artifact in sorted(args.artifacts.glob("*")):
			if artifact.suffix == ".whl" or artifact.name.endswith((".tar.gz", ".tgz", ".zip")):
				findings.extend(_scan_entries(artifact.name, _archive_files(artifact)))
	if not args.git_tree and args.artifacts is None:
		parser.error("specify --git-tree or --artifacts")
	if findings:
		print("release privacy scan failed:")
		for finding in findings:
			print(f"- {finding}")
		return 1
	print("release privacy scan passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
