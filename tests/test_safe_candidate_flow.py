"""受限求职入口的离线安全契约测试。"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from boss_agent_cli.safe_cli import SafeApplyError, collect_candidates, execute_batch_items
from boss_agent_cli.safe_browser_worker import BrowserWorkerError
from boss_agent_cli.safe_config import (
	DEFAULT_BATCH_LIMIT,
	DEFAULT_DAILY_ATTEMPT_LIMIT,
	DEFAULT_MAX_SEARCH_REQUESTS,
	ApplicantProfile,
	BrowserConfig,
	SEARCH_TRACKS,
	SafeApplyConfig,
	make_search_profile,
	save_config,
	validate_message,
)
from boss_agent_cli.safe_store import SafeStore, today_key


_TEST_APPLICANT = ApplicantProfile(
	school="四川大学",
	major="计算机科学与技术",
	graduation_year=2028,
	city="成都",
	job_type="实习",
	availability="可立即到岗",
	min_internship_months=6,
)
_TEST_PROFILES = tuple(
	make_search_profile(
		track.id,
		f"具备{track.label}相关课程与项目实践",
		(f"{track.label}课程项目",),
	)
	for track in SEARCH_TRACKS
)


def _config(**overrides: Any) -> SafeApplyConfig:
	return replace(
		SafeApplyConfig(
			applicant=_TEST_APPLICANT,
			profiles=_TEST_PROFILES,
			browser=BrowserConfig(),
		),
		**overrides,
	)


@pytest.fixture(autouse=True)
def _initialized_config(tmp_path: Path) -> None:
	save_config(tmp_path, _config(), backup_existing=False)


def _raw_job(
	job_id: str,
	*,
	title: str = "AI Agent 开发实习",
	company: str = "测试科技",
	query_hint: str = "Agent",
	job_type: int = 4,
	city: str = "成都",
	security_id: str | None = None,
	skills: list[str] | None = None,
	scale: str = "100-499人",
) -> dict[str, Any]:
	return {
		"encryptJobId": job_id,
		"jobName": title,
		"brandName": company,
		"salaryDesc": "150-200/天",
		"cityName": city,
		"areaDistrict": "高新区",
		"jobExperience": "在校生",
		"jobDegree": "本科",
		"skills": skills or [query_hint, "Python"],
		"welfareList": [],
		"brandIndustry": "互联网",
		"brandScaleName": scale,
		"brandStageName": "A轮",
		"bossName": "测试HR",
		"bossTitle": "HR",
		"bossOnline": True,
		"securityId": security_id or f"security-{job_id}",
		"jobType": job_type,
		"jobLabels": [],
	}


class FakePlatform:
	def __init__(self, results: dict[str, list[dict[str, Any]]]) -> None:
		self.results = results
		self.search_calls: list[tuple[str, dict[str, Any]]] = []
		self.greet_calls: list[tuple[str, str, str]] = []
		self.apply_calls: list[tuple[Any, ...]] = []
		self.user_info_calls = 0
		self.greet_result: dict[str, Any] = {"code": 0, "zpData": {}}
		self.greet_side_effect: Any = None

	def __enter__(self) -> "FakePlatform":
		return self

	def __exit__(self, *args: Any) -> None:
		return None

	def search_jobs(self, query: str, **filters: Any) -> dict[str, Any]:
		self.search_calls.append((query, filters))
		return {"code": 0, "zpData": {"jobList": self.results.get(query, [])}}

	def user_info(self) -> dict[str, Any]:
		self.user_info_calls += 1
		return {"code": 0, "zpData": {"name": "offline"}}

	def unwrap_data(self, response: dict[str, Any]) -> Any:
		return response.get("zpData")

	def is_success(self, response: dict[str, Any]) -> bool:
		return response.get("code") == 0

	def parse_error(self, response: dict[str, Any]) -> tuple[str, str]:
		return str(response.get("error_code", "UNKNOWN")), str(response.get("message", "普通错误"))

	def greet(self, security_id: str, job_id: str, message: str) -> dict[str, Any]:
		self.greet_calls.append((security_id, job_id, message))
		if self.greet_side_effect is not None:
			return self.greet_side_effect(security_id, job_id, message)
		return self.greet_result

	def apply(self, *args: Any) -> dict[str, Any]:
		self.apply_calls.append(args)
		raise AssertionError("安全入口不得调用 apply")


def _candidate(job_id: str, *, company: str = "测试科技") -> dict[str, Any]:
	return {
		"job_id": job_id,
		"security_id": f"security-{job_id}",
		"title": "AI Agent 开发实习",
		"company": company,
		"city": "成都",
		"district": "高新区",
		"salary": "150-200/天",
		"track_id": "agent",
		"track_label": "Agent 应用",
		"query": "AI Agent",
		"message": _TEST_PROFILES[0].render_message(_TEST_APPLICANT),
	}


def _batch(store: SafeStore, *job_ids: str) -> dict[str, Any]:
	return store.get_batch(store.create_batch([_candidate(job_id) for job_id in job_ids], "digest", 30)) or {}


def test_safe_config_has_hard_limits_and_no_placeholder_messages() -> None:
	config = _config()
	config.validate()
	assert config.batch_limit == DEFAULT_BATCH_LIMIT == 20
	assert config.daily_attempt_limit == DEFAULT_DAILY_ATTEMPT_LIMIT == 150
	assert config.max_search_requests == DEFAULT_MAX_SEARCH_REQUESTS == 20
	assert config.city == "成都"
	assert config.job_type == "实习"
	assert all(not validate_message(profile.render_message(config.applicant)) for profile in config.profiles)
	assert all(profile.resume_evidence for profile in config.profiles)
	assert "RAG" not in config.profiles[0].render_message(config.applicant)
	assert "LLM应用开发" not in config.profiles[1].render_message(config.applicant)
	assert {profile.id for profile in config.profiles} == {
		"agent", "llm", "cv", "fullstack", "frontend", "research", "algorithm", "backend", "test", "other",
	}
	with pytest.raises(ValueError):
		_config(batch_limit=21).validate()
	with pytest.raises(ValueError):
		_config(daily_attempt_limit=151).validate()
	with pytest.raises(ValueError):
		_config(write_delay=(14, 30)).validate()
	with pytest.raises(ValueError):
		_config(company_per_batch=2).validate()
	_config(company_per_day=3).validate()
	with pytest.raises(ValueError):
		_config(company_per_day=151).validate()
	with pytest.raises(ValueError):
		_config(daily_attempt_limit=50, company_per_day=51).validate()


def test_collect_candidates_filters_non_target_roles_without_blocking_test_development(tmp_path: Path) -> None:
	platform = FakePlatform(
		{
			"AI Agent": [
				_raw_job("job-ok", title="AI Agent 测试开发实习生", company="目标科技", query_hint="Agent"),
				_raw_job("job-ops", title="AI 产品运营实习生", company="运营公司", query_hint="Agent"),
				_raw_job("job-pm", title="AI 产品经理实习生", company="产品公司", query_hint="Agent"),
				_raw_job("job-label", title="大模型数据标注实习生", company="标注公司", query_hint="Agent"),
				_raw_job("job-support", title="AI 技术支持实习生", company="支持公司", query_hint="Agent"),
				_raw_job("job-onsite", title="Python 开发实习生（驻场）", company="外包公司", query_hint="Agent"),
			]
		}
	)
	with SafeStore(tmp_path) as store:
		candidates, requests, errors = collect_candidates(platform, store, _config(), max_requests=1)

	assert requests == 1
	assert errors == []
	assert [item["job_id"] for item in candidates] == ["job-ok"]


def test_collect_candidates_uses_fixed_priority_filters_and_read_only_search(tmp_path: Path) -> None:
	platform = FakePlatform(
		{
			"AI Agent": [
				_raw_job("job-agent", query_hint="Agent"),
				_raw_job("job-sales", title="AI Agent 销售实习", company="销售公司", query_hint="Agent"),
				_raw_job("job-fulltime", job_type=1, query_hint="Agent"),
			],
			"智能体开发": [
				_raw_job("job-agent", query_hint="Agent"),
				_raw_job("job-backend", title="Python 后端开发实习", company="另一科技", query_hint="Agent", skills=["AI Agent", "Python", "后端"]),
			],
			"大模型": [_raw_job("job-shanghai", city="上海", query_hint="大模型")],
		}
	)
	with SafeStore(tmp_path) as store:
		candidates, requests, errors = collect_candidates(platform, store, _config())

	assert errors == []
	assert requests == DEFAULT_MAX_SEARCH_REQUESTS
	assert [query for query, _ in platform.search_calls] == [
		query for profile in _TEST_PROFILES for query in profile.queries
	]
	assert all(filters == {"city": "成都", "job_type": "实习"} for _, filters in platform.search_calls)
	assert [item["job_id"] for item in candidates] == ["job-agent", "job-backend"]
	assert len(platform.greet_calls) == 0
	assert len(platform.apply_calls) == 0
	assert all(not validate_message(item["message"]) for item in candidates)


def test_collect_candidates_prefers_larger_companies_without_reducing_batch_capacity(tmp_path: Path) -> None:
	jobs = [
		_raw_job(
			f"job-small-{index}",
			company=f"小型公司{index}",
			query_hint="Agent",
			scale="20-99人",
		)
		for index in range(20)
	]
	jobs.append(
		_raw_job(
			"job-large",
			company="大型公司",
			query_hint="Agent",
			scale="10000人以上",
		)
	)
	platform = FakePlatform({"AI Agent": jobs})

	with SafeStore(tmp_path) as store:
		candidates, requests, errors = collect_candidates(
			platform,
			store,
			_config(max_search_requests=1),
		)

	assert requests == 1
	assert errors == []
	assert len(candidates) == 20
	assert candidates[0]["job_id"] == "job-large"
	assert "job-small-19" not in {item["job_id"] for item in candidates}
	assert platform.greet_calls == []
	assert platform.apply_calls == []


def test_collect_candidates_aborts_on_first_network_error_without_retry(tmp_path: Path) -> None:
	class FailingSearchPlatform(FakePlatform):
		def search_jobs(self, query: str, **filters: Any) -> dict[str, Any]:
			self.search_calls.append((query, filters))
			return {"code": -1, "error_code": "NETWORK_ERROR", "message": "BROWSER_FETCH_TIMEOUT"}

	platform = FailingSearchPlatform({})
	with SafeStore(tmp_path) as store:
		with pytest.raises(SafeApplyError) as error:
			collect_candidates(platform, store, _config())

	assert error.value.code == "NETWORK_ERROR"
	assert error.value.stop is False
	assert [query for query, _ in platform.search_calls] == ["AI Agent"]


def test_store_encrypts_security_id_and_expiry_clears_it(tmp_path: Path) -> None:
	with SafeStore(tmp_path) as store:
		batch_id = store.create_batch([_candidate("job-secret")], "digest", 1)
		batch = store.get_batch(batch_id)
		assert batch is not None
		assert batch["items"][0]["security_id"] == "security-job-secret"
		cipher_db = (tmp_path / "safe_apply.db").read_bytes()
		assert b"security-job-secret" not in cipher_db
		expires_at = float(batch["expires_at"])
		assert store.expire_batches(expires_at + 1) == 1
		expired = store.get_batch(batch_id)
		assert expired is not None
		assert expired["status"] == "EXPIRED"
		assert expired["items"][0]["security_id"] is None


def test_store_reserves_before_write_and_enforces_daily_and_company_limits(tmp_path: Path) -> None:
	with SafeStore(tmp_path) as store:
		batch_id = store.create_batch([_candidate("job-1"), _candidate("job-2")], "digest", 30)
		allowed, reason, attempt_id = store.reserve_attempt(
			batch_id, 1, "job-1", "测试科技", daily_limit=150, batch_limit=20, company_limit=1
		)
		assert (allowed, reason) == (True, "RESERVED")
		assert attempt_id is not None
		store.finish_attempt(attempt_id, batch_id, 1, "sent")
		assert store.daily_attempts() == 1
		allowed, reason, _ = store.reserve_attempt(
			batch_id, 2, "job-2", "测试科技", daily_limit=150, batch_limit=20, company_limit=1
		)
		assert (allowed, reason) == (False, "COMPANY_LIMIT")

		# 额度按“尝试数”计数，达到 150 后即使前面的请求失败也不能再保留名额。
		store._conn.executemany(  # noqa: SLF001 - 仅用于离线构造额度边界
			"INSERT INTO write_attempts (day_key, batch_id, position, job_id, company_key, attempted_at) VALUES (?, ?, ?, ?, ?, ?)",
			[(today_key(), "seed", n, f"seed-{n}", f"seed-company-{n}", time.time()) for n in range(149)],
		)
		store._conn.commit()  # noqa: SLF001
		allowed, reason, _ = store.reserve_attempt(
			batch_id, 2, "job-2", "另一科技", daily_limit=150, batch_limit=20, company_limit=2
		)
		assert (allowed, reason) == (False, "DAILY_LIMIT")


def test_execute_calls_greet_once_per_job_with_no_apply_and_clears_secrets(tmp_path: Path) -> None:
	platform = FakePlatform({})
	sleeps: list[float] = []
	with SafeStore(tmp_path) as store:
		batch = _batch(store, "job-1", "job-2")
		result = execute_batch_items(
			store,
			batch,
			_config(write_delay=(15, 30)),
			platform,
			sleep=sleeps.append,
			random_uniform=lambda low, high: 17,
		)
		assert result["attempted"] == 2
		assert result["sent"] == 2
		assert [call[1] for call in platform.greet_calls] == ["job-1", "job-2"]
		assert all(call[2].startswith("HR您好，我是四川大学计算机科学与技术2028届本科生") for call in platform.greet_calls)
		assert platform.apply_calls == []
		assert sleeps == [17]
		assert store.daily_attempts() == 2
		assert store.get_batch(batch["batch_id"])["status"] == "COMPLETED"
		assert all(item["security_id"] is None for item in store.get_batch(batch["batch_id"])["items"])


def test_execute_does_not_retry_timeout_and_does_not_charge_unexecuted_item(tmp_path: Path) -> None:
	platform = FakePlatform({})
	call_count = {"job-1": 0, "job-2": 0}

	def greet(security_id: str, job_id: str, message: str) -> dict[str, Any]:
		call_count[job_id] += 1
		if job_id == "job-1":
			raise TimeoutError("network timeout")
		return {"code": 0, "zpData": {}}

	platform.greet_side_effect = greet
	with SafeStore(tmp_path) as store:
		batch = _batch(store, "job-1", "job-2")
		store.exclude_positions(batch["batch_id"], [2])
		result = execute_batch_items(store, store.get_batch(batch["batch_id"]) or {}, _config(), platform, sleep=lambda _: None)
		assert result["unknown"] == 1
		assert result["attempted"] == 1
		assert call_count == {"job-1": 1, "job-2": 0}
		assert store.daily_attempts() == 1
		assert store.has_attempted_job("job-2") is False


def test_execute_greet_hang_is_unknown_once_then_stops_and_clears_all_security_ids(tmp_path: Path) -> None:
	platform = FakePlatform({})
	platform.greet_side_effect = lambda *_: (_ for _ in ()).throw(BrowserWorkerError("BROWSER_HUNG", "greet"))
	with SafeStore(tmp_path) as store:
		batch = _batch(store, "job-1", "job-2")
		result = execute_batch_items(store, batch, _config(), platform, sleep=lambda _: None)

		assert result["stopped_reason"] == "BROWSER_HUNG"
		assert result["unknown"] == 1
		assert result["attempted"] == 1
		assert len(platform.greet_calls) == 1
		assert store.daily_attempts() == 1
		assert store.circuit_breaker() is None
		assert store.probe_gate()["required"] is True
		assert store.probe_gate()["passed"] is False
		final_batch = store.get_batch(batch["batch_id"])
		assert final_batch is not None
		assert all(item["security_id"] is None for item in final_batch["items"])


def test_execute_network_response_is_unknown_once_then_stops_and_clears_all_security_ids(tmp_path: Path) -> None:
	platform = FakePlatform({})
	platform.greet_result = {"code": -1, "error_code": "NETWORK_ERROR", "message": "BROWSER_FETCH_TIMEOUT_15000MS"}
	with SafeStore(tmp_path) as store:
		batch = _batch(store, "job-1", "job-2")
		result = execute_batch_items(store, batch, _config(), platform, sleep=lambda _: None)

		assert result["stopped_reason"] == "NETWORK_ERROR"
		assert result["unknown"] == 1
		assert result["failed"] == 0
		assert result["attempted"] == 1
		assert len(platform.greet_calls) == 1
		assert store.daily_attempts() == 1
		assert store.circuit_breaker() is None
		assert store.probe_gate()["required"] is True
		assert store.probe_gate()["passed"] is False
		final_batch = store.get_batch(batch["batch_id"])
		assert final_batch is not None
		assert all(item["security_id"] is None for item in final_batch["items"])


@pytest.mark.parametrize("error_code", ["BROWSER_HUNG", "BROWSER_PAGE_NOT_READY", "BROWSER_ERROR"])
def test_probe_browser_technical_failure_aborts_without_breaker_batch_or_write(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	error_code: str,
) -> None:
	from boss_agent_cli import safe_cli

	class HungProbePlatform(FakePlatform):
		def search_jobs(self, query: str, **filters: Any) -> dict[str, Any]:
			self.search_calls.append((query, filters))
			raise BrowserWorkerError(error_code, "search_jobs")

	platform = HungProbePlatform({})
	monkeypatch.setattr(safe_cli, "build_platform_instance", lambda *args, **kwargs: platform)
	monkeypatch.setattr(safe_cli, "AuthManager", lambda *args, **kwargs: object())
	with SafeStore(tmp_path) as store:
		store.acknowledge_risk("offline test")
	result = CliRunner().invoke(safe_cli.cli, ["--data-dir", str(tmp_path), "preview", "--probe"])

	assert result.exit_code != 0
	assert error_code in result.output
	assert len(platform.search_calls) == 1
	assert platform.greet_calls == []
	with SafeStore(tmp_path) as store:
		assert store.daily_attempts() == 0
		assert store.circuit_breaker() is None
		assert store.probe_gate()["required"] is True
		assert store.probe_gate()["passed"] is False
		assert store._conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0  # noqa: SLF001


def test_risk_response_opens_persistent_breaker_and_stops_remaining(tmp_path: Path) -> None:
	platform = FakePlatform({})
	platform.greet_side_effect = lambda *_: {"code": 400, "error_code": "RATE_LIMITED", "message": "rate limited"}
	with SafeStore(tmp_path) as store:
		batch = _batch(store, "job-1", "job-2")
		result = execute_batch_items(store, batch, _config(), platform, sleep=lambda _: None)
		assert result["stopped_reason"] == "RATE_LIMITED"
		assert len(platform.greet_calls) == 1
		assert store.daily_attempts() == 1
		assert store.circuit_breaker()["open"] is True


def test_environment_risk_write_stops_batch_once_without_global_breaker(tmp_path: Path) -> None:
	platform = FakePlatform({})
	platform.greet_result = {"code": 37, "error_code": "ENVIRONMENT_RISK", "message": "您的环境存在异常"}
	with SafeStore(tmp_path) as store:
		batch = _batch(store, "job-1", "job-2")
		result = execute_batch_items(store, batch, _config(), platform, sleep=lambda _: None)

		assert result == {
			"batch_id": batch["batch_id"],
			"sent": 0,
			"failed": 1,
			"unknown": 0,
			"attempted": 1,
			"stopped_reason": "ENVIRONMENT_RISK",
		}
		assert len(platform.greet_calls) == 1
		assert store.daily_attempts() == 1
		assert store.circuit_breaker() is None
		assert store.probe_gate()["required"] is True
		final_batch = store.get_batch(batch["batch_id"])
		assert final_batch is not None
		assert final_batch["status"] == "STOPPED"
		assert all(item["security_id"] is None for item in final_batch["items"])


def test_first_ordinary_write_error_stops_batch_without_breaker_or_retry(tmp_path: Path) -> None:
	platform = FakePlatform({})
	platform.greet_side_effect = lambda *_: {"code": 500, "error_code": "UNKNOWN", "message": "ordinary error"}
	with SafeStore(tmp_path) as store:
		batch = _batch(store, "job-1", "job-2", "job-3")
		result = execute_batch_items(store, batch, _config(), platform, sleep=lambda _: None)
		assert result["stopped_reason"] == "UNKNOWN"
		assert len(platform.greet_calls) == 1
		assert [call[1] for call in platform.greet_calls] == ["job-1"]
		assert store.daily_attempts() == 1
		assert store.circuit_breaker() is None
		assert store.probe_gate()["required"] is True


def test_safe_cli_exposes_only_restricted_commands() -> None:
	from boss_agent_cli.safe_cli import cli

	assert set(cli.commands) == {
		"init", "doctor", "login", "preview", "execute", "status", "history", "stop", "acknowledge-risk", "browser-stop"
	}


def test_status_exposes_query_bound_probe_and_scheduler_state(tmp_path: Path) -> None:
	from boss_agent_cli.safe_cli import cli

	runner = CliRunner()
	fresh = runner.invoke(cli, ["--data-dir", str(tmp_path), "status"])
	assert fresh.exit_code == 0, fresh.output
	assert json.loads(fresh.output)["data"]["probe_gate"] == {
		"required": False,
		"passed": False,
		"expires_at": None,
		"query_key": None,
		"query": None,
		"consumed_at": None,
	}
	fresh_data = json.loads(fresh.output)["data"]
	assert fresh_data["query_schedule"]["next_query"] == "AI Agent"
	assert fresh_data["search_cooldown_remaining_seconds"] == 0
	assert fresh_data["recovery_gate"]["required"] is False

	with SafeStore(tmp_path) as store:
		store.acknowledge_risk("offline manual check")
	required = runner.invoke(cli, ["--data-dir", str(tmp_path), "status"])
	assert required.exit_code == 0, required.output
	gate = json.loads(required.output)["data"]["probe_gate"]
	assert gate["required"] is True
	assert gate["passed"] is False


def test_preview_cli_is_read_only_and_has_no_apply_or_chat_surface(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	from boss_agent_cli import safe_cli

	platform = FakePlatform({"AI Agent": [_raw_job("job-cli", query_hint="Agent")]})
	monkeypatch.setattr(safe_cli, "build_platform_instance", lambda *args, **kwargs: platform)
	monkeypatch.setattr(safe_cli, "AuthManager", lambda *args, **kwargs: object())
	monkeypatch.setattr(safe_cli, "READ_SEARCH_INTERVAL_SECONDS", 0)
	runner = CliRunner()
	with SafeStore(tmp_path) as store:
		store.acknowledge_risk("offline test")
	probe = runner.invoke(safe_cli.cli, ["--data-dir", str(tmp_path), "preview", "--probe"])
	assert probe.exit_code == 0, probe.output
	result = runner.invoke(safe_cli.cli, ["--data-dir", str(tmp_path), "preview"])
	assert result.exit_code == 0, result.output
	data = json.loads(result.output)
	assert data["count"] == 1
	assert data["search_requests"] == 1
	assert [query for query, _ in platform.search_calls] == ["AI Agent", "AI Agent"]
	assert platform.greet_calls == []
	assert platform.apply_calls == []
	assert "security_id" not in result.output


def test_preview_probe_calls_one_search_and_does_not_create_batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	from boss_agent_cli import safe_cli

	platform = FakePlatform({"AI Agent": [_raw_job("job-probe", query_hint="Agent")]})
	monkeypatch.setattr(safe_cli, "build_platform_instance", lambda *args, **kwargs: platform)
	monkeypatch.setattr(safe_cli, "AuthManager", lambda *args, **kwargs: object())
	runner = CliRunner()

	result = runner.invoke(safe_cli.cli, ["--data-dir", str(tmp_path), "preview", "--probe"])

	assert result.exit_code == 0, result.output
	data = json.loads(result.output)
	assert data["ok"] is True
	assert data["mode"] == "probe"
	assert data["search_requests"] == 1
	assert data["batch_id"] is None
	assert len(platform.search_calls) == 1
	assert platform.greet_calls == []
	with SafeStore(tmp_path) as store:
		assert store._conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0  # noqa: SLF001
		gate = store.probe_gate()
		assert gate["required"] is True
		assert gate["passed"] is True


def test_preview_and_probe_are_rejected_while_circuit_breaker_is_open(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	from boss_agent_cli import safe_cli

	with SafeStore(tmp_path) as store:
		store.open_circuit_breaker("manual safety stop")
	platform_builds: list[bool] = []
	monkeypatch.setattr(safe_cli, "build_platform_instance", lambda *args, **kwargs: platform_builds.append(True))
	runner = CliRunner()

	for args in (["preview"], ["preview", "--probe"]):
		result = runner.invoke(safe_cli.cli, ["--data-dir", str(tmp_path), *args])
		assert result.exit_code != 0
		assert "安全熔断已打开" in result.output
	assert platform_builds == []


def test_probe_ordinary_network_error_does_not_open_circuit_breaker(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	from boss_agent_cli import safe_cli

	class ProbeFailurePlatform(FakePlatform):
		def search_jobs(self, query: str, **filters: Any) -> dict[str, Any]:
			self.search_calls.append((query, filters))
			return {"code": -1, "error_code": "NETWORK_ERROR", "message": "network timeout"}

	platform = ProbeFailurePlatform({})
	monkeypatch.setattr(safe_cli, "build_platform_instance", lambda *args, **kwargs: platform)
	monkeypatch.setattr(safe_cli, "AuthManager", lambda *args, **kwargs: object())
	runner = CliRunner()
	with SafeStore(tmp_path) as store:
		store.acknowledge_risk("offline test")
		store.mark_probe_passed(_config().digest(), "agent:0", "AI Agent", 30)
		assert store.probe_gate()["passed"] is True

	result = runner.invoke(safe_cli.cli, ["--data-dir", str(tmp_path), "preview", "--probe"])

	assert result.exit_code != 0
	assert len(platform.search_calls) == 1
	with SafeStore(tmp_path) as store:
		assert store.circuit_breaker() is None
		assert store.probe_gate()["passed"] is False
		assert store.recovery_gate()["required"] is True
		assert store._conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0  # noqa: SLF001


def test_probe_platform_risk_aborts_without_persistent_breaker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	from boss_agent_cli import safe_cli

	class RiskProbePlatform(FakePlatform):
		def search_jobs(self, query: str, **filters: Any) -> dict[str, Any]:
			self.search_calls.append((query, filters))
			return {"code": 37, "error_code": "ENVIRONMENT_RISK", "message": "您的环境存在异常"}

	platform = RiskProbePlatform({})
	monkeypatch.setattr(safe_cli, "build_platform_instance", lambda *args, **kwargs: platform)
	monkeypatch.setattr(safe_cli, "AuthManager", lambda *args, **kwargs: object())
	monkeypatch.setattr(safe_cli, "READ_SEARCH_INTERVAL_SECONDS", 0)
	with SafeStore(tmp_path) as store:
		store.acknowledge_risk("offline test")

	result = CliRunner().invoke(safe_cli.cli, ["--data-dir", str(tmp_path), "preview", "--probe"])

	assert result.exit_code != 0
	assert "ENVIRONMENT_RISK" in result.output
	assert len(platform.search_calls) == 1
	with SafeStore(tmp_path) as store:
		assert store.circuit_breaker() is None
		assert store.probe_gate()["passed"] is False
		assert store.recovery_gate()["required"] is True
		schedule = store.query_schedule(_config().digest(), [{"key": "agent:0", "query": "AI Agent"}])
		assert schedule["blocked_today"][0]["query_key"] == "agent:0"
		assert store.daily_attempts() == 0
		assert store._conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0  # noqa: SLF001


@pytest.mark.parametrize("error_code", ["CAPTCHA", "ACCOUNT_RISK"])
def test_probe_platform_challenge_aborts_without_breaker(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	error_code: str,
) -> None:
	from boss_agent_cli import safe_cli

	class ChallengePlatform(FakePlatform):
		def search_jobs(self, query: str, **filters: Any) -> dict[str, Any]:
			self.search_calls.append((query, filters))
			return {"code": 36, "error_code": error_code, "message": "需要人工处理"}

	platform = ChallengePlatform({})
	monkeypatch.setattr(safe_cli, "build_platform_instance", lambda *args, **kwargs: platform)
	monkeypatch.setattr(safe_cli, "AuthManager", lambda *args, **kwargs: object())
	with SafeStore(tmp_path) as store:
		store.acknowledge_risk("offline test")

	result = CliRunner().invoke(safe_cli.cli, ["--data-dir", str(tmp_path), "preview", "--probe"])

	assert result.exit_code != 0
	assert error_code in result.output
	assert len(platform.search_calls) == 1
	with SafeStore(tmp_path) as store:
		assert store.circuit_breaker() is None
		assert store.probe_gate()["passed"] is False
		assert store.recovery_gate()["required"] is True
		assert store.daily_attempts() == 0


def test_probe_explicit_token_expiry_still_opens_breaker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	from boss_agent_cli import safe_cli

	class ExpiredTokenPlatform(FakePlatform):
		def search_jobs(self, query: str, **filters: Any) -> dict[str, Any]:
			self.search_calls.append((query, filters))
			return {"code": 37, "error_code": "TOKEN_REFRESH_FAILED", "message": "stoken 已过期"}

	platform = ExpiredTokenPlatform({})
	monkeypatch.setattr(safe_cli, "build_platform_instance", lambda *args, **kwargs: platform)
	monkeypatch.setattr(safe_cli, "AuthManager", lambda *args, **kwargs: object())
	with SafeStore(tmp_path) as store:
		store.acknowledge_risk("offline test")

	result = CliRunner().invoke(safe_cli.cli, ["--data-dir", str(tmp_path), "preview", "--probe"])

	assert result.exit_code != 0
	assert "TOKEN_REFRESH_FAILED" in result.output
	assert len(platform.search_calls) == 1
	with SafeStore(tmp_path) as store:
		assert store.circuit_breaker()["open"] is True
		assert store.recovery_gate()["required"] is True
		assert store.daily_attempts() == 0


def test_probe_gate_requires_success_then_allows_exactly_one_preview(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	from boss_agent_cli import safe_cli

	platform = FakePlatform({"AI Agent": [_raw_job("job-gated", query_hint="Agent")]})
	monkeypatch.setattr(safe_cli, "build_platform_instance", lambda *args, **kwargs: platform)
	monkeypatch.setattr(safe_cli, "AuthManager", lambda *args, **kwargs: object())
	monkeypatch.setattr(safe_cli, "READ_SEARCH_INTERVAL_SECONDS", 0)
	with SafeStore(tmp_path) as store:
		store.open_circuit_breaker("offline test")
		store.acknowledge_risk("offline manual check")
	runner = CliRunner()

	blocked = runner.invoke(safe_cli.cli, ["--data-dir", str(tmp_path), "preview"])
	assert blocked.exit_code != 0
	assert "探针门禁未通过" in blocked.output
	assert platform.search_calls == []

	probe = runner.invoke(safe_cli.cli, ["--data-dir", str(tmp_path), "preview", "--probe"])
	assert probe.exit_code == 0, probe.output
	with SafeStore(tmp_path) as store:
		assert store.probe_gate()["passed"] is True

	preview = runner.invoke(safe_cli.cli, ["--data-dir", str(tmp_path), "preview"])
	assert preview.exit_code == 0, preview.output
	assert json.loads(preview.output)["count"] == 1
	calls_after_preview = len(platform.search_calls)
	with SafeStore(tmp_path) as store:
		gate = store.probe_gate()
		assert gate["required"] is True
		assert gate["passed"] is False
		assert gate["consumed_at"] is not None

	second = runner.invoke(safe_cli.cli, ["--data-dir", str(tmp_path), "preview"])
	assert second.exit_code != 0
	assert "探针门禁未通过" in second.output
	assert len(platform.search_calls) == calls_after_preview


def test_probe_gate_rejects_digest_mismatch_and_expiry_and_consumes_once(tmp_path: Path) -> None:
	with SafeStore(tmp_path) as store:
		store.require_probe(timestamp=100.0)
		assert store.consume_probe_pass("digest", "agent:0", "AI Agent", timestamp=101.0) is False

		store.mark_probe_passed("digest", "agent:0", "AI Agent", 1, timestamp=100.0)
		assert store.probe_gate(timestamp=120.0)["passed"] is True
		assert store.consume_probe_pass("other", "agent:0", "AI Agent", timestamp=120.0) is False
		assert store.consume_probe_pass("digest", "agent:1", "智能体开发", timestamp=120.0) is False
		assert store.consume_probe_pass("digest", "agent:0", "AI Agent", timestamp=161.0) is False

		store.mark_probe_passed("digest", "agent:0", "AI Agent", 1, timestamp=200.0)
		assert store.consume_probe_pass("digest", "agent:0", "AI Agent", timestamp=220.0) is True
		assert store.consume_probe_pass("digest", "agent:0", "AI Agent", timestamp=221.0) is False
		gate = store.probe_gate(timestamp=221.0)
		assert gate["required"] is True
		assert gate["passed"] is False
		assert gate["consumed_at"] == 220.0


def test_search_cooldown_is_persistent_and_does_not_consume_probe_early(tmp_path: Path) -> None:
	with SafeStore(tmp_path) as store:
		assert store.reserve_probe_search("agent:0", "AI Agent", 60, timestamp=100.0) == 0
		store.mark_probe_passed("digest", "agent:0", "AI Agent", 30, timestamp=100.0)
		status, remaining = store.begin_preview_search(
			"digest", "agent:0", "AI Agent", 60, timestamp=120.0
		)
		assert (status, remaining) == ("cooldown", 40)
		assert store.probe_gate(timestamp=120.0)["passed"] is True

	with SafeStore(tmp_path) as reopened:
		assert reopened.search_cooldown_remaining(60, timestamp=130.0) == 30
		status, remaining = reopened.begin_preview_search(
			"digest", "agent:0", "AI Agent", 60, timestamp=160.0
		)
		assert (status, remaining) == ("ok", 0)
		assert reopened.probe_gate(timestamp=160.0)["passed"] is False


def test_legacy_probe_without_query_binding_cannot_authorize_preview(tmp_path: Path) -> None:
	with SafeStore(tmp_path) as store:
		store._set_state(  # noqa: SLF001
			"probe_gate",
			{"required": True, "passed": True, "config_digest": "digest", "expires_at": 1000.0},
		)
		status, remaining = store.begin_preview_search(
			"digest", "agent:0", "AI Agent", 60, timestamp=200.0
		)
		assert (status, remaining) == ("probe_required", 0)


def test_query_schedule_rotates_blocks_and_resets_daily_sets(tmp_path: Path) -> None:
	catalog = [
		{"key": "agent:0", "query": "AI Agent"},
		{"key": "agent:1", "query": "智能体开发"},
		{"key": "llm:0", "query": "大模型"},
	]
	started = 1_780_000_000.0
	with SafeStore(tmp_path) as store:
		assert store.choose_query_key("digest", catalog, timestamp=started) == "agent:0"
		store.mark_query_completed("digest", catalog, "agent:0", timestamp=started)
		assert store.choose_query_key("digest", catalog, timestamp=started) == "agent:1"
		store.block_query(
			"digest", catalog, "agent:1", "智能体开发", "ENVIRONMENT_RISK", timestamp=started + 1
		)
		schedule = store.query_schedule("digest", catalog, timestamp=started + 2)
		assert schedule["next_query_key"] == "llm:0"
		assert schedule["completed_today"] == ["agent:0"]
		assert schedule["blocked_today"][0]["code"] == "ENVIRONMENT_RISK"
		store.block_query("digest", catalog, "llm:0", "大模型", "ENVIRONMENT_RISK", timestamp=started + 3)
		assert store.query_schedule("digest", catalog, timestamp=started + 4)["next_query_key"] is None

		next_day = started + 86_400
		rolled = store.query_schedule("digest", catalog, timestamp=next_day)
		assert rolled["completed_today"] == []
		assert rolled["blocked_today"] == []
		assert rolled["next_query_key"] == "agent:0"
		assert store.query_schedule("other-digest", catalog, timestamp=next_day)["cursor"] == 0


def test_exact_query_probe_and_preview_are_bound_to_same_single_search(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	from boss_agent_cli import safe_cli

	platform = FakePlatform({
		"计算机视觉": [_raw_job("job-cv", title="计算机视觉算法实习", query_hint="计算机视觉")],
	})
	monkeypatch.setattr(safe_cli, "build_platform_instance", lambda *args, **kwargs: platform)
	monkeypatch.setattr(safe_cli, "AuthManager", lambda *args, **kwargs: object())
	monkeypatch.setattr(safe_cli, "READ_SEARCH_INTERVAL_SECONDS", 0)
	with SafeStore(tmp_path) as store:
		store.acknowledge_risk("offline exact query test")
	runner = CliRunner()

	probe = runner.invoke(
		safe_cli.cli,
		["--data-dir", str(tmp_path), "preview", "--probe", "--query", "计算机视觉"],
	)
	assert probe.exit_code == 0, probe.output
	probe_data = json.loads(probe.output)
	assert probe_data["query_key"] == "cv:0"
	assert probe_data["query"] == "计算机视觉"

	preview = runner.invoke(safe_cli.cli, ["--data-dir", str(tmp_path), "preview"])
	assert preview.exit_code == 0, preview.output
	preview_data = json.loads(preview.output)
	assert preview_data["query_key"] == "cv:0"
	assert preview_data["count"] == 1
	assert [query for query, _ in platform.search_calls] == ["计算机视觉", "计算机视觉"]
	assert platform.greet_calls == []
	assert platform.apply_calls == []


def test_query_option_rejects_unconfigured_or_ordinary_override_without_platform_call(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	from boss_agent_cli import safe_cli

	platform = FakePlatform({})
	monkeypatch.setattr(safe_cli, "build_platform_instance", lambda *args, **kwargs: platform)
	monkeypatch.setattr(safe_cli, "AuthManager", lambda *args, **kwargs: object())
	with SafeStore(tmp_path) as store:
		store.acknowledge_risk("offline validation")
	runner = CliRunner()

	invalid = runner.invoke(
		safe_cli.cli,
		["--data-dir", str(tmp_path), "preview", "--probe", "--query", "任意放宽关键词"],
	)
	assert invalid.exit_code != 0
	override = runner.invoke(
		safe_cli.cli,
		["--data-dir", str(tmp_path), "preview", "--query", "AI Agent"],
	)
	assert override.exit_code != 0
	assert platform.search_calls == []


def test_status_live_uses_restricted_platform_user_info_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	from boss_agent_cli import safe_cli

	class FakeAuth:
		def check_status(self) -> object:
			return object()

	platform = FakePlatform({})
	monkeypatch.setattr(safe_cli, "build_platform_instance", lambda *args, **kwargs: platform)
	monkeypatch.setattr(safe_cli, "AuthManager", lambda *args, **kwargs: FakeAuth())
	result = CliRunner().invoke(safe_cli.cli, ["--data-dir", str(tmp_path), "status", "--live"])

	assert result.exit_code == 0, result.output
	assert json.loads(result.output)["ok"] is True
	assert platform.user_info_calls == 1
	assert platform.search_calls == []
	assert platform.greet_calls == []


def test_execute_requires_exact_confirmation_and_then_greets_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	from boss_agent_cli import safe_cli

	with SafeStore(tmp_path) as store:
		batch_id = store.create_batch([_candidate("job-confirm")], "digest", 30)

	platform = FakePlatform({})
	monkeypatch.setattr(safe_cli, "build_platform_instance", lambda *args, **kwargs: platform)
	monkeypatch.setattr(safe_cli, "AuthManager", lambda *args, **kwargs: object())
	monkeypatch.setattr(safe_cli, "_is_interactive_terminal", lambda: True)
	runner = CliRunner()

	wrong = runner.invoke(safe_cli.cli, ["--data-dir", str(tmp_path), "execute", batch_id], input="\n不确认\n")
	assert wrong.exit_code == 0, wrong.output
	assert platform.greet_calls == []
	with SafeStore(tmp_path) as store:
		assert store.daily_attempts() == 0
		assert store.get_batch(batch_id)["status"] == "PREVIEWED"

	confirmed = runner.invoke(
		safe_cli.cli,
		["--data-dir", str(tmp_path), "execute", batch_id],
		input=f"\n确认投递 {batch_id}\n",
	)
	assert confirmed.exit_code == 0, confirmed.output
	assert len(platform.greet_calls) == 1
	with SafeStore(tmp_path) as store:
		assert store.daily_attempts() == 1
