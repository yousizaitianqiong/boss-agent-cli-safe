"""版本化公开配置与交互初始化的离线契约测试。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from click.testing import CliRunner

from boss_agent_cli import safe_config
from boss_agent_cli.safe_cli import cli
from boss_agent_cli.safe_config import (
	ApplicantProfile,
	BrowserConfig,
	SafeApplyConfig,
	SafeConfigError,
	config_path,
	config_state,
	load_config,
	make_search_profile,
	save_config,
)


def _applicant(**overrides: object) -> ApplicantProfile:
	base = ApplicantProfile(
		school="四川大学",
		major="计算机科学与技术",
		graduation_year=2028,
		city="成都",
		job_type="实习",
		availability="可立即到岗",
		min_internship_months=6,
	)
	return replace(base, **overrides)


def _profile(*, claim: str = "具备智能体应用课程与项目实践", evidence: tuple[str, ...] = ("智能体课程项目",)):
	return make_search_profile("agent", claim, evidence)


def _config(**overrides: object) -> SafeApplyConfig:
	base = SafeApplyConfig(applicant=_applicant(), profiles=(_profile(),), browser=BrowserConfig())
	return replace(base, **overrides)


def _first_init_input(*, confirm: str = "y") -> str:
	return "\n".join([
		"",
		"计算机科学与技术",
		"",
		"成都",
		"可立即到岗",
		"6",
		"agent",
		"具备智能体应用课程与项目实践",
		"智能体课程项目",
		"",
		"",
		"",
		confirm,
	]) + "\n"


def _rerun_input(*, confirm: str = "y") -> str:
	return "\n".join(["", "", "", "", "", "", "", "", "", "", "", "", confirm]) + "\n"


def test_init_completes_schema_v1_without_platform_access(tmp_path: Path) -> None:
	result = CliRunner().invoke(cli, ["--data-dir", str(tmp_path), "init"], input=_first_init_input())

	assert result.exit_code == 0, result.output
	assert config_state(tmp_path) == "ready"
	config = load_config(tmp_path)
	assert config.applicant.school == "四川大学"
	assert config.applicant.major == "计算机科学与技术"
	assert config.applicant.graduation_year == 2028
	assert config.applicant.job_type == "实习"
	assert [profile.id for profile in config.profiles] == ["agent"]
	assert config.company_per_day == config.daily_attempt_limit == 150
	assert config.search_interval_seconds == 60


def test_init_cancel_does_not_create_or_change_config(tmp_path: Path) -> None:
	cancelled = CliRunner().invoke(cli, ["--data-dir", str(tmp_path), "init"], input=_first_init_input(confirm="n"))
	assert cancelled.exit_code == 0, cancelled.output
	assert config_path(tmp_path).exists() is False

	original = _config()
	save_config(tmp_path, original, backup_existing=False)
	before = config_path(tmp_path).read_bytes()
	cancelled_again = CliRunner().invoke(cli, ["--data-dir", str(tmp_path), "init"], input=_rerun_input(confirm="n"))
	assert cancelled_again.exit_code == 0, cancelled_again.output
	assert config_path(tmp_path).read_bytes() == before


def test_init_rerun_uses_existing_values_and_writes_backup(tmp_path: Path) -> None:
	save_config(tmp_path, _config(), backup_existing=False)
	result = CliRunner().invoke(cli, ["--data-dir", str(tmp_path), "init"], input=_rerun_input())

	assert result.exit_code == 0, result.output
	backups = list(tmp_path.glob("safe-config.backup-*.json"))
	assert len(backups) == 1
	assert load_config(tmp_path).applicant.major == "计算机科学与技术"


def test_legacy_init_migrates_limits_with_backup_and_keeps_other_state(tmp_path: Path) -> None:
	legacy = {
		"city": "成都",
		"batch_limit": 12,
		"daily_attempt_limit": 90,
		"write_delay": [18, 35],
		"batch_cooldown_minutes": 12,
		"plan_ttl_minutes": 20,
		"max_search_requests": 8,
		"company_per_batch": 1,
		"company_per_day": 90,
		"search_interval_seconds": 75,
	}
	tmp_path.mkdir(parents=True, exist_ok=True)
	config_path(tmp_path).write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
	marker = tmp_path / "safe_apply.db"
	marker.write_bytes(b"existing-state")

	result = CliRunner().invoke(cli, ["--data-dir", str(tmp_path), "init"], input=_first_init_input())

	assert result.exit_code == 0, result.output
	migrated = load_config(tmp_path)
	assert (migrated.batch_limit, migrated.daily_attempt_limit, migrated.max_search_requests) == (12, 90, 8)
	assert migrated.write_delay == (18.0, 35.0)
	assert migrated.search_interval_seconds == 75
	backup = next(tmp_path.glob("safe-config.backup-*.json"))
	assert json.loads(backup.read_text(encoding="utf-8")) == legacy
	assert marker.read_bytes() == b"existing-state"


def test_save_config_is_atomic_when_replace_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	original = _config()
	save_config(tmp_path, original, backup_existing=False)
	before = config_path(tmp_path).read_bytes()
	changed = replace(original, applicant=replace(original.applicant, availability="两周内到岗"))
	monkeypatch.setattr(safe_config.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("replace failed")))

	with pytest.raises(OSError):
		save_config(tmp_path, changed, backup_existing=False)

	assert config_path(tmp_path).read_bytes() == before
	assert list(tmp_path.glob(".safe-config.json.*.tmp")) == []


@pytest.mark.parametrize(
	("overrides", "expected"),
	[
		({"major": ""}, "major"),
		({"city": "不存在城市"}, "不支持的城市"),
		({"job_type": "应届"}, "日常实习"),
		({"min_internship_months": 0}, "1-24"),
	],
)
def test_applicant_required_fields_are_validated(overrides: dict[str, object], expected: str) -> None:
	with pytest.raises(SafeConfigError, match=expected):
		_applicant(**overrides).validate()


def test_track_claim_evidence_unknown_track_and_message_length_are_validated() -> None:
	with pytest.raises(SafeConfigError, match="未知技术方向"):
		make_search_profile("imaginary", "真实技能", ("简历证据",))
	with pytest.raises(SafeConfigError, match="claim"):
		replace(_config(), profiles=(_profile(claim="短"),)).validate()
	with pytest.raises(SafeConfigError, match="resume_evidence"):
		replace(_config(), profiles=(_profile(evidence=()),)).validate()
	with pytest.raises(SafeConfigError, match="招呼语无效"):
		replace(_config(), profiles=(_profile(claim="项目" * 40),)).validate()


def test_unconfigured_platform_commands_fail_but_local_diagnostics_work(tmp_path: Path) -> None:
	runner = CliRunner()
	login = runner.invoke(cli, ["--data-dir", str(tmp_path), "login"])
	status = runner.invoke(cli, ["--data-dir", str(tmp_path), "status"])
	browser_stop = runner.invoke(cli, ["--data-dir", str(tmp_path), "browser-stop"])

	assert login.exit_code != 0
	assert "boss-safe init" in login.output
	assert status.exit_code == 0, status.output
	assert json.loads(status.output)["data"]["config_state"] == "missing"
	assert browser_stop.exit_code == 0, browser_stop.output
