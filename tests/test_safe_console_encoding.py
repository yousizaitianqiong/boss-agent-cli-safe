from __future__ import annotations

from boss_agent_cli import safe_cli


class _Stream:
	encoding = "cp1252"

	def __init__(self) -> None:
		self.calls: list[dict[str, str]] = []

	def reconfigure(self, **kwargs: str) -> None:
		self.calls.append(kwargs)


def test_windows_console_encoding_is_configured_before_click_help(monkeypatch) -> None:
	stdout = _Stream()
	stderr = _Stream()
	monkeypatch.setattr(safe_cli.os, "name", "nt")
	monkeypatch.setattr(safe_cli.sys, "stdout", stdout)
	monkeypatch.setattr(safe_cli.sys, "stderr", stderr)

	safe_cli._configure_windows_console_encoding()  # noqa: SLF001

	assert stdout.calls == [{"encoding": "utf-8", "errors": "backslashreplace"}]
	assert stderr.calls == [{"encoding": "utf-8", "errors": "backslashreplace"}]
