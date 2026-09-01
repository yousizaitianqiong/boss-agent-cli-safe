"""面向求职者的受限安全投递 CLI。

该入口只做：已配置城市的日常实习职位只读搜索、批次预览、人工确认后的
一次立即沟通。它不注册聊天、招聘者、跟进或自动回复命令，也不会连接用户
日常浏览器。
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import click

from boss_agent_cli.api.client import AccountRiskError
from boss_agent_cli.api.models import JobItem
from boss_agent_cli.auth.manager import AuthManager, AuthRequired, TokenRefreshFailed
from boss_agent_cli.auth.token_store import TokenStore
from boss_agent_cli.commands._platform import build_platform_instance
from boss_agent_cli.output import Logger
from boss_agent_cli.safe_config import (
	ApplicantProfile,
	BrowserConfig,
	NON_TECH_TERMS,
	SEARCH_TRACKS,
	SafeApplyConfig,
	SafeConfigError,
	SearchProfile,
	company_key,
	config_state,
	default_data_dir,
	legacy_limit_defaults,
	load_config,
	make_search_profile,
	normalize_text,
	read_config_document,
	save_config,
	validate_message,
)
from boss_agent_cli.safe_browser_worker import (
	record_diagnostic,
	run_login_in_worker,
)
from boss_agent_cli.safe_browser_worker import BrowserWorkerError, SafeBrowserPlatform
from boss_agent_cli.safe_edge import (
	BrowserRuntimeError,
	browser_profile_path,
	browser_runtime_status,
	ensure_browser_running,
	find_browser_executable,
	stop_browser,
)
from boss_agent_cli.safe_store import SafeStore, now_ts


def _configure_windows_console_encoding() -> None:
	"""让 Windows 的非 UTF-8 控制台也能显示安全 CLI 的中文帮助与诊断。"""
	if os.name != "nt":
		return
	for stream in (sys.stdout, sys.stderr):
		reconfigure = getattr(stream, "reconfigure", None)
		if not callable(reconfigure):
			continue
		encoding = str(getattr(stream, "encoding", "") or "").replace("_", "-").casefold()
		if encoding == "utf-8":
			continue
		try:
			reconfigure(encoding="utf-8", errors="backslashreplace")
		except (OSError, ValueError):
			# 被宿主替换的流可能不支持重新配置；保留其原有行为而不是阻止 CLI 启动。
			pass


_configure_windows_console_encoding()


_DEFAULT_BUILD_PLATFORM_INSTANCE = build_platform_instance
_LOCAL_COMMANDS_WITHOUT_CONFIG = {"init", "doctor", "status", "history", "stop", "browser-stop"}
# 仅作为离线测试注入点；生产运行始终读取经校验的版本化配置。
READ_SEARCH_INTERVAL_SECONDS: int | None = None

_READ_ONLY_BREAKER_CODES = {
	"AUTH_EXPIRED",
	"AUTH_REQUIRED",
	"TOKEN_REFRESH_FAILED",
	"RATE_LIMITED",
	"GREET_LIMIT",
	"BROWSER_PROFILE_NOT_CLEAN",
}
_READ_ONLY_NON_BREAKER_ABORT_CODES = {
	"BROWSER_HUNG",
	"BROWSER_PAGE_NOT_READY",
	"BROWSER_ERROR",
	"NETWORK_ERROR",
	"ENVIRONMENT_RISK",
	"ACCOUNT_RISK",
	"CAPTCHA",
}
_READ_ONLY_ABORT_CODES = _READ_ONLY_BREAKER_CODES | _READ_ONLY_NON_BREAKER_ABORT_CODES
_WRITE_BREAKER_CODES = {
	"AUTH_EXPIRED",
	"AUTH_REQUIRED",
	"TOKEN_REFRESH_FAILED",
	"RATE_LIMITED",
	"GREET_LIMIT",
	"BROWSER_PROFILE_NOT_CLEAN",
}
_WRITE_UNCERTAIN_CODES = {
	"BROWSER_HUNG",
	"BROWSER_PAGE_NOT_READY",
	"BROWSER_ERROR",
	"NETWORK_ERROR",
	"UNKNOWN",
}


class SafeApplyError(RuntimeError):
	def __init__(self, code: str, message: str, *, stop: bool = False) -> None:
		self.code = code
		self.stop = stop
		super().__init__(message)


@dataclass(frozen=True, slots=True)
class QuerySpec:
	key: str
	index: int
	profile: SearchProfile
	query: str


_SENSITIVE_ERROR_PATTERN = re.compile(
	r"(?i)(security[_-]?id|token|stoken|cookie|session)\s*[=:]\s*['\"]?[^,\s}\"']+"
)


def _redact_message(value: Any) -> str:
	"""错误只保留短摘要，避免审计或终端输出泄露临时凭据。"""
	text = str(value)
	text = _SENSITIVE_ERROR_PATTERN.sub(lambda match: f"{match.group(1)}=<redacted>", text)
	return text[:240]


def _data_dir(ctx: click.Context) -> Path:
	return ctx.ensure_object(dict)["data_dir"]


def _ctx(ctx: click.Context) -> tuple[Path, SafeApplyConfig]:
	obj = ctx.ensure_object(dict)
	config = obj.get("config")
	if not isinstance(config, SafeApplyConfig):
		raise click.ClickException("安全配置尚未就绪；请先运行 boss-safe init")
	return obj["data_dir"], config


@contextmanager
def _isolated_runtime(data_dir: Path | None = None) -> Any:
	"""安全入口禁止自动刷新与通道重试；真实浏览器 URL 只在 worker 内注入。"""
	previous = os.environ.get("BOSS_SAFE_ISOLATED_BROWSER")
	previous_profile = os.environ.get("BOSS_SAFE_BROWSER_PROFILE")
	os.environ["BOSS_SAFE_ISOLATED_BROWSER"] = "1"
	if data_dir is not None:
		os.environ["BOSS_SAFE_BROWSER_PROFILE"] = str(browser_profile_path(data_dir))
	try:
		yield
	finally:
		if previous is None:
			os.environ.pop("BOSS_SAFE_ISOLATED_BROWSER", None)
		else:
			os.environ["BOSS_SAFE_ISOLATED_BROWSER"] = previous
		if previous_profile is None:
			os.environ.pop("BOSS_SAFE_BROWSER_PROFILE", None)
		else:
			os.environ["BOSS_SAFE_BROWSER_PROFILE"] = previous_profile


def _echo_json(value: Any) -> None:
	click.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _error_code_from_exception(exc: BaseException) -> str:
	if isinstance(exc, BrowserWorkerError):
		return exc.code
	if isinstance(exc, BrowserRuntimeError):
		return exc.code
	if isinstance(exc, AccountRiskError):
		return "ACCOUNT_RISK"
	if isinstance(exc, AuthRequired):
		return "AUTH_REQUIRED"
	if isinstance(exc, TokenRefreshFailed):
		return "TOKEN_REFRESH_FAILED"
	if isinstance(exc, KeyboardInterrupt) or isinstance(exc, TimeoutError):
		return "BROWSER_HUNG"
	message = str(exc)
	lower = message.lower()
	normalized = lower.replace("-", "_").replace(" ", "_")
	if any(term in normalized for term in ("token_expired", "stoken_expired", "token已过期", "stoken已过期")):
		return "TOKEN_REFRESH_FAILED"
	if "您的环境存在异常" in message or "environment_risk" in normalized:
		return "ENVIRONMENT_RISK"
	if "greet_limit" in normalized:
		return "GREET_LIMIT"
	if "rate_limited" in normalized:
		return "RATE_LIMITED"
	if "account_risk" in normalized:
		return "ACCOUNT_RISK"
	if "auth_expired" in normalized:
		return "AUTH_EXPIRED"
	if "token_refresh_failed" in normalized:
		return "TOKEN_REFRESH_FAILED"
	if any(term in lower for term in ("验证码", "安全验证", "captcha", "verify")):
		return "CAPTCHA"
	if any(term in lower for term in ("风控", "risk", "forbidden", "异常访问")):
		return "ACCOUNT_RISK"
	if any(term in lower for term in ("频率", "限流", "rate", "too many")):
		return "RATE_LIMITED"
	if any(term in lower for term in ("登录", "未登录", "token", "stoken", "unauthorized")):
		return "AUTH_EXPIRED"
	if any(term in lower for term in ("timeout", "timed out", "network", "connection", "网络", "连接")):
		return "NETWORK_ERROR"
	return "UNKNOWN"


def _safe_platform(data_dir: Path) -> Any:
	"""真实安全命令走 worker；测试替身仍可注入离线平台。"""
	if build_platform_instance is not _DEFAULT_BUILD_PLATFORM_INSTANCE:
		return build_platform_instance("zhipin", AuthManager(data_dir, platform="zhipin"), delay=(2.0, 5.0), cdp_url=None)
	return SafeBrowserPlatform(data_dir, delay=(2.0, 5.0))


def _query_specs(config: SafeApplyConfig) -> tuple[QuerySpec, ...]:
	result: list[QuerySpec] = []
	for profile in config.profiles:
		for query_index, query in enumerate(profile.queries):
			result.append(QuerySpec(f"{profile.id}:{query_index}", len(result), profile, query))
	return tuple(result[:config.max_search_requests])


def _query_catalog(config: SafeApplyConfig) -> list[dict[str, str]]:
	return [{"key": item.key, "query": item.query} for item in _query_specs(config)]


def _search_interval(config: SafeApplyConfig) -> int:
	return config.search_interval_seconds if READ_SEARCH_INTERVAL_SECONDS is None else READ_SEARCH_INTERVAL_SECONDS


def _query_by_exact_text(config: SafeApplyConfig, query: str) -> QuerySpec:
	for item in _query_specs(config):
		if item.query == query:
			return item
	available = "、".join(item.query for item in _query_specs(config))
	raise click.ClickException(f"--query 只能使用现有安全关键词；可选：{available}")


def _query_by_key(config: SafeApplyConfig, query_key: str) -> QuerySpec | None:
	for item in _query_specs(config):
		if item.key == query_key:
			return item
	return None


def _error_message(response: dict[str, Any], fallback: str) -> str:
	return _redact_message(response.get("message") or response.get("msg") or response.get("error") or fallback)


def _job_list(platform: Any, response: dict[str, Any]) -> list[dict[str, Any]]:
	data = platform.unwrap_data(response) or {}
	if isinstance(data, list):
		return [item for item in data if isinstance(item, dict)]
	if not isinstance(data, dict):
		return []
	for key in ("jobList", "list", "items", "result"):
		items = data.get(key)
		if isinstance(items, list):
			return [item for item in items if isinstance(item, dict)]
	return []


def _profile_matches(item: JobItem, profile: SearchProfile) -> bool:
	text = normalize_text(item.title, item.skills, item.industry, item.job_labels)
	if profile.id == "other":
		return any(term in text for term in profile.match_terms)
	return any(term.lower() in text for term in profile.match_terms)


def _reject_reason(item: JobItem, profile: SearchProfile, config: SafeApplyConfig) -> str | None:
	if not item.job_id or not item.security_id:
		return "缺少稳定职位 ID 或临时安全 ID"
	if not item.city.startswith(config.city):
		return f"城市不是{config.city}"
	if item.employment_type != "实习" and str(item.raw_job_type) != "4":
		return "职位类型不是实习"
	text = normalize_text(item.title, item.company, item.skills, item.industry)
	for term in NON_TECH_TERMS:
		if term.lower() in text:
			return f"命中排除词: {term}"
	if ("硕士" in item.education or "博士" in item.education) and "本科" not in item.education:
		return "学历要求高于本科"
	if not _profile_matches(item, profile):
		return f"未匹配 {profile.label} 方向关键词"
	message_reasons = validate_message(profile.render_message(config.applicant))
	if message_reasons:
		return "招呼语校验失败: " + "; ".join(message_reasons)
	return None


def _company_scale_rank(value: str) -> int:
	"""把 BOSS 公司规模标签转换为稳定的优先级；未知规模排在已知规模之后。"""
	match = re.search(r"\d+", value.replace(",", ""))
	return int(match.group(0)) if match else -1


def _public_candidate(
	item: JobItem,
	profile: SearchProfile,
	query: str,
	config: SafeApplyConfig,
) -> dict[str, Any]:
	return {
		"job_id": item.job_id,
		"security_id": item.security_id,
		"title": item.title,
		"company": item.company,
		"city": item.city,
		"district": item.district,
		"salary": item.salary,
		"track_id": profile.id,
		"track_label": profile.label,
		"query": query,
		"message": profile.render_message(config.applicant),
	}


def collect_candidates(
	platform: Any,
	store: SafeStore,
	config: SafeApplyConfig,
	*,
	max_requests: int | None = None,
	query_specs: tuple[QuerySpec, ...] | None = None,
) -> tuple[list[dict[str, Any]], int, list[dict[str, str]]]:
	"""按固定优先级收集候选；只依赖只读 search_jobs。"""
	selected: list[dict[str, Any]] = []
	seen_job_ids: set[str] = set()
	company_counts: dict[str, int] = {}
	errors: list[dict[str, str]] = []
	consecutive_errors = 0
	request_limit = max_requests or config.max_search_requests
	requests = 0
	search_plan = query_specs or _query_specs(config)

	for spec in search_plan:
		profile = spec.profile
		query = spec.query
		if requests >= request_limit or len(selected) >= config.batch_limit:
			return selected, requests, errors
		requests += 1
		try:
			response = platform.search_jobs(query, city=config.city, job_type=config.job_type)
		except Exception as exc:  # 平台错误必须停止当前预览，不尝试换通道
			code = _error_code_from_exception(exc)
			errors.append({"query": query, "code": code, "message": _redact_message(exc)})
			if code in _READ_ONLY_ABORT_CODES:
				raise SafeApplyError(
					code,
					f"搜索 {query} 失败: {_redact_message(exc)}",
					stop=code in _READ_ONLY_BREAKER_CODES,
				) from exc
			consecutive_errors += 1
			if consecutive_errors >= 2:
				return selected, requests, errors
			continue
		if not platform.is_success(response):
			code, message = platform.parse_error(response)
			message = _redact_message(message)
			errors.append({"query": query, "code": code, "message": message})
			if code in _READ_ONLY_ABORT_CODES:
				raise SafeApplyError(
					code,
					f"搜索 {query} 失败: {message}",
					stop=code in _READ_ONLY_BREAKER_CODES,
				)
			consecutive_errors += 1
			if consecutive_errors >= 2:
				return selected, requests, errors
			continue
		consecutive_errors = 0

		items = [JobItem.from_api(raw_item) for raw_item in _job_list(platform, response)]
		items.sort(key=lambda item: _company_scale_rank(item.scale), reverse=True)
		for item in items:
			if item.job_id in seen_job_ids or store.has_attempted_job(item.job_id):
				continue
			seen_job_ids.add(item.job_id)
			if _reject_reason(item, profile, config) is not None:
				continue
			key = company_key(item.company)
			if not key or company_counts.get(key, 0) >= config.company_per_batch:
				continue
			candidate = _public_candidate(item, profile, query, config)
			selected.append(candidate)
			company_counts[key] = company_counts.get(key, 0) + 1
			if len(selected) >= config.batch_limit:
				return selected, requests, errors

	return selected, requests, errors


def _cooldown_remaining(store: SafeStore, config: SafeApplyConfig) -> int:
	last = store.last_completed_at()
	if last is None:
		return 0
	return max(0, int(last + config.batch_cooldown_minutes * 60 - now_ts()))


def _record_read_failure(
	store: SafeStore,
	config: SafeApplyConfig,
	catalog: list[dict[str, str]],
	spec: QuerySpec,
	code: str,
	message: str,
	*,
	open_breaker: bool,
) -> None:
	if code == "ENVIRONMENT_RISK":
		store.block_query(config.digest(), catalog, spec.key, spec.query, code)
	if open_breaker:
		store.open_circuit_breaker(f"search {spec.query}: {code}: {_redact_message(message)}")
	store.require_recovery(code, query_key=spec.key, query=spec.query)


def _parse_positions(raw: str, maximum: int) -> list[int]:
	if not raw.strip():
		return []
	positions: set[int] = set()
	for token in raw.split(","):
		try:
			position = int(token.strip())
		except ValueError as exc:
			raise click.ClickException(f"无效的排除编号: {token}") from exc
		if not 1 <= position <= maximum:
			raise click.ClickException(f"排除编号必须在 1-{maximum} 之间")
		positions.add(position)
	return sorted(positions)


def _is_interactive_terminal() -> bool:
	return sys.stdin.isatty() and sys.stdout.isatty()


def execute_batch_items(
	store: SafeStore,
	batch: dict[str, Any],
	config: SafeApplyConfig,
	platform: Any,
	*,
	sleep: Callable[[float], None] = time.sleep,
	random_uniform: Callable[[float, float], float] = random.uniform,
) -> dict[str, Any]:
	"""执行已人工确认的批次；每个职位最多发起一次写请求。"""
	batch_id = str(batch["batch_id"])
	store.begin_batch(batch_id)
	sent = 0
	failed = 0
	unknown = 0
	stopped_reason = ""
	items = [item for item in batch["items"] if item["status"] == "pending"]

	for index, item in enumerate(items):
		company = company_key(str(item["company"]))
		allowed, reason, attempt_id = store.reserve_attempt(
			batch_id,
			int(item["position"]),
			str(item["job_id"]),
			company,
			daily_limit=config.daily_attempt_limit,
			batch_limit=config.batch_limit,
			company_limit=config.company_per_day,
		)
		if not allowed:
			stopped_reason = reason
			break
		assert attempt_id is not None
		security_id = item.get("security_id")
		if not security_id:
			store.finish_attempt(attempt_id, batch_id, int(item["position"]), "unknown", "SECURITY_ID_UNAVAILABLE")
			unknown += 1
			stopped_reason = "SECURITY_ID_UNAVAILABLE"
			store.require_recovery("SECURITY_ID_UNAVAILABLE")
			break

		try:
			response = platform.greet(str(security_id), str(item["job_id"]), str(item["message"]))
			if platform.is_success(response):
				store.finish_attempt(attempt_id, batch_id, int(item["position"]), "sent")
				sent += 1
			else:
				code, message = platform.parse_error(response)
				if code in _WRITE_UNCERTAIN_CODES:
					store.finish_attempt(attempt_id, batch_id, int(item["position"]), "unknown", code)
					unknown += 1
				else:
					store.finish_attempt(attempt_id, batch_id, int(item["position"]), "failed", code)
					failed += 1
				stopped_reason = code or "UNKNOWN"
				if code in _WRITE_BREAKER_CODES:
					store.open_circuit_breaker(f"batch {batch_id}: {code}: {_redact_message(message)}")
				# 平台挑战或结果不确定只停止本批次；后续必须取得新恢复授权和探针。
				store.require_recovery(code or "UNKNOWN")
				break
		except (Exception, KeyboardInterrupt) as exc:
			code = _error_code_from_exception(exc)
			store.finish_attempt(attempt_id, batch_id, int(item["position"]), "unknown", code)
			unknown += 1
			stopped_reason = code
			if code in _WRITE_BREAKER_CODES:
				store.open_circuit_breaker(f"batch {batch_id}: {code}: {_redact_message(exc)}")
			store.require_recovery(code)
			break

		if index < len(items) - 1 and not stopped_reason:
			sleep(random_uniform(*config.write_delay))

	if stopped_reason:
		store.finish_batch(batch_id, "STOPPED", stopped_reason)
	else:
		store.finish_batch(batch_id, "COMPLETED")
	return {
		"batch_id": batch_id,
		"sent": sent,
		"failed": failed,
		"unknown": unknown,
		"attempted": sent + failed + unknown,
		"stopped_reason": stopped_reason or None,
	}


def _prompt_required(label: str, *, default: str | None = None) -> str:
	while True:
		value = click.prompt(label, default=default, show_default=default is not None, type=str).strip()
		if value:
			return value
		click.echo("该项不能为空。")


def _parse_track_selection(value: str) -> tuple[str, ...]:
	selected = tuple(dict.fromkeys(item.strip().lower() for item in value.split(",") if item.strip()))
	available = {track.id for track in SEARCH_TRACKS}
	invalid = [item for item in selected if item not in available]
	if invalid:
		raise SafeConfigError(f"未知技术方向：{', '.join(invalid)}")
	if not selected:
		raise SafeConfigError("至少选择一个技术方向")
	return selected


def _limits_from_existing(existing: SafeApplyConfig | None, raw: dict[str, Any] | None) -> dict[str, Any]:
	if existing is None:
		return legacy_limit_defaults(raw)
	return {
		"batch_limit": existing.batch_limit,
		"daily_attempt_limit": existing.daily_attempt_limit,
		"write_delay": existing.write_delay,
		"batch_cooldown_minutes": existing.batch_cooldown_minutes,
		"plan_ttl_minutes": existing.plan_ttl_minutes,
		"max_search_requests": existing.max_search_requests,
		"company_per_batch": existing.company_per_batch,
		"company_per_day": existing.company_per_day,
		"search_interval_seconds": existing.search_interval_seconds,
	}


@click.group("boss-safe")
@click.option("--data-dir", type=click.Path(path_type=Path, file_okay=False), default=None, help="安全投递数据目录")
@click.pass_context
def cli(ctx: click.Context, data_dir: Path | None) -> None:
	"""BOSS 直聘求职安全投递入口。"""
	ctx.ensure_object(dict)
	resolved = data_dir or default_data_dir()
	resolved.mkdir(parents=True, exist_ok=True)
	ctx.obj["data_dir"] = resolved
	ctx.obj["config_state"] = config_state(resolved)
	ctx.obj["config"] = None
	ctx.obj["config_error"] = None
	try:
		ctx.obj["config"] = load_config(resolved)
	except SafeConfigError as exc:
		ctx.obj["config_error"] = str(exc)
	ctx.obj["logger"] = Logger("error")
	ctx.obj["platform"] = "zhipin"
	if (
		ctx.invoked_subcommand
		and ctx.invoked_subcommand not in _LOCAL_COMMANDS_WITHOUT_CONFIG
		and ctx.obj["config"] is None
	):
		raise click.ClickException(f"安全配置尚未就绪：{ctx.obj['config_error']}；请运行 boss-safe init")


@cli.command("init")
@click.pass_context
def init_cmd(ctx: click.Context) -> None:
	"""交互创建或迁移本机安全配置；不访问浏览器或 BOSS。"""
	data_dir = _data_dir(ctx)
	raw = read_config_document(data_dir)
	existing: SafeApplyConfig | None = None
	if isinstance(ctx.obj.get("config"), SafeApplyConfig):
		existing = ctx.obj["config"]
	applicant = existing.applicant if existing is not None else None
	browser = existing.browser if existing is not None else BrowserConfig()
	profiles = {profile.id: profile for profile in existing.profiles} if existing is not None else {}
	limits = _limits_from_existing(existing, raw)

	click.echo("此向导只写入本机配置，不登录、不搜索、不发送招呼。")
	click.echo("学校和届别可使用共同默认值；专业、城市、到岗信息和技能证据必须由本人确认。")
	school = _prompt_required("学校", default=applicant.school if applicant else "四川大学")
	major = _prompt_required("专业全称", default=applicant.major if applicant else None)
	graduation_year = click.prompt(
		"毕业年份",
		default=applicant.graduation_year if applicant else 2028,
		type=click.IntRange(2020, 2100),
	)
	legacy_city = str((raw or {}).get("city") or "").strip() or None
	city = _prompt_required("目标城市", default=applicant.city if applicant else legacy_city)
	availability = _prompt_required("到岗说明", default=applicant.availability if applicant else None)
	if applicant is not None:
		months = click.prompt("可连续实习月数", default=applicant.min_internship_months, type=click.IntRange(1, 24))
	else:
		months = click.prompt("可连续实习月数", type=click.IntRange(1, 24))

	click.echo("可选方向：" + "；".join(f"{track.id}={track.label}" for track in SEARCH_TRACKS))
	default_tracks = ",".join(profiles) if profiles else None
	while True:
		try:
			selected_ids = _parse_track_selection(_prompt_required("启用方向 ID（逗号分隔）", default=default_tracks))
			break
		except SafeConfigError as exc:
			click.echo(str(exc))
	configured_profiles: list[SearchProfile] = []
	for track_id in selected_ids:
		current = profiles.get(track_id)
		click.echo(f"配置方向：{track_id}")
		claim = _prompt_required("  可由简历证明的技能表述", default=current.claim if current else None)
		evidence_default = "，".join(current.resume_evidence) if current else None
		while True:
			evidence_text = _prompt_required("  简历证据（多条用逗号分隔）", default=evidence_default)
			evidence = tuple(item.strip() for item in re.split(r"[,，]", evidence_text) if item.strip())
			if evidence:
				break
			click.echo("至少填写一条简历证据。")
		configured_profiles.append(make_search_profile(track_id, claim, evidence))

	browser_kind = click.prompt(
		"专用浏览器类型",
		default=browser.kind,
		type=click.Choice(["auto", "edge", "chrome", "chromium"], case_sensitive=False),
	)
	executable = click.prompt(
		"浏览器可执行文件（留空=自动发现）",
		default=browser.executable or "",
		show_default=False,
		type=str,
	).strip() or None
	port = click.prompt("本地 CDP 端口", default=browser.cdp_port, type=click.IntRange(1024, 65535))

	config = SafeApplyConfig(
		applicant=ApplicantProfile(
			school=school,
			major=major,
			graduation_year=graduation_year,
			city=city,
			job_type="实习",
			availability=availability,
			min_internship_months=months,
		),
		profiles=tuple(configured_profiles),
		browser=BrowserConfig(kind=browser_kind, executable=executable, cdp_port=port),
		**limits,
	)
	try:
		config.validate()
	except SafeConfigError as exc:
		raise click.ClickException(f"配置校验失败：{exc}；原配置未改变") from exc

	click.echo("将生成以下招呼语：")
	for profile in config.profiles:
		click.echo(f"[{profile.label}] {profile.render_message(config.applicant)}")
	if not click.confirm("确认以上信息均真实且写入本机配置？", default=False):
		_echo_json({"ok": False, "cancelled": True, "message": "已取消；原配置未改变"})
		return
	backup = save_config(data_dir, config, backup_existing=True)
	ctx.obj["config"] = config
	ctx.obj["config_state"] = "ready"
	_echo_json({
		"ok": True,
		"config_state": "ready",
		"config_path": str(data_dir / "safe-config.json"),
		"backup_path": str(backup) if backup else None,
		"enabled_tracks": [profile.id for profile in config.profiles],
		"next": "运行 boss-safe doctor",
	})


@cli.command("doctor")
@click.pass_context
def doctor_cmd(ctx: click.Context) -> None:
	"""只做本地诊断，不读取浏览器 Cookie，不访问 BOSS。"""
	data_dir = _data_dir(ctx)
	config = ctx.obj.get("config")
	state = str(ctx.obj.get("config_state") or "invalid")
	checks = []
	python_ok = sys.version_info >= (3, 10)
	checks.append({"name": "python", "ok": python_ok, "detail": sys.version.split()[0]})
	try:
		import patchright  # noqa: F401

		patchright_ok = True
	except ImportError:
		patchright_ok = False
	checks.append({"name": "patchright", "ok": patchright_ok, "detail": "已安装" if patchright_ok else "未安装"})
	found = find_browser_executable(config.browser if isinstance(config, SafeApplyConfig) else BrowserConfig())
	checks.append({
		"name": "browser",
		"ok": found is not None,
		"detail": f"{found.kind}: {found.path}" if found is not None else "未找到可用 Chromium 系浏览器",
	})
	runtime = browser_runtime_status(data_dir, config.browser if isinstance(config, SafeApplyConfig) else None)
	runtime_integrity = bool(
		not runtime.get("runtime_invalid")
		and not runtime.get("inspection_error")
		and (runtime["owned"] or not runtime["running"])
	)
	checks.append({
		"name": "strict_cdp_runtime",
		"ok": runtime_integrity,
		"detail": (
			"专用浏览器已运行且 CDP 可用"
			if runtime["cdp_ready"]
			else ("专用浏览器未运行；login 将启动" if not runtime["running"] else "profile 进程归属无法验证")
		),
	})
	checks.append({"name": "browser_fallback", "ok": True, "detail": "严格 CDP；禁止 Bridge、APIRequestContext 和无头回退"})
	checks.append({"name": "data_dir", "ok": data_dir.exists() and os.access(data_dir, os.W_OK), "detail": str(data_dir)})
	checks.append({
		"name": "config",
		"ok": isinstance(config, SafeApplyConfig),
		"detail": (
			f"{config.city}/{config.job_type}，{len(config.profiles)} 个方向，每日硬上限 {config.daily_attempt_limit}"
			if isinstance(config, SafeApplyConfig)
			else f"{state}；请运行 boss-safe init"
		),
	})
	_echo_json({"ok": all(item["ok"] for item in checks), "checks": checks})


@cli.command("login")
@click.option("--timeout", default=120, type=click.IntRange(min=30, max=600), show_default=True)
@click.pass_context
def login_cmd(ctx: click.Context, timeout: int) -> None:
	"""启动或复用专用可见浏览器，并通过严格 CDP 保存登录态。"""
	data_dir, config = _ctx(ctx)
	started = time.monotonic()
	browser_ready = False
	try:
		cdp_url = ensure_browser_running(data_dir, config.browser)
		browser_ready = True
		record_diagnostic(data_dir, stage="browser_launch", operation="login", started=started, error_code="OK")
		token = run_login_in_worker(data_dir, cdp_url=cdp_url, timeout=timeout, platform="zhipin")
		TokenStore(data_dir / "auth").save(token)
	except (Exception, KeyboardInterrupt) as exc:
		code = _error_code_from_exception(exc)
		record_diagnostic(
			data_dir,
			stage="login" if browser_ready else "browser_launch",
			operation="login",
			started=started,
			error_code=code,
		)
		raise click.ClickException(f"{code}: {_redact_message(exc)}") from exc
	_echo_json({
		"ok": True,
		"message": "登录态已保存；专用浏览器将保持运行供后续严格 CDP 命令复用",
		"data_dir": str(data_dir),
		"browser_runtime": browser_runtime_status(data_dir, config.browser),
	})


@cli.command("status")
@click.option("--live", is_flag=True, default=False, help="执行一次只读用户信息验证")
@click.pass_context
def status_cmd(ctx: click.Context, live: bool) -> None:
	"""查看登录态、今日额度和熔断状态。"""
	data_dir = _data_dir(ctx)
	config = ctx.obj.get("config")
	if live and not isinstance(config, SafeApplyConfig):
		raise click.ClickException("实时状态需要完整配置；请先运行 boss-safe init")
	with SafeStore(data_dir) as store:
		store.expire_batches()
		daily = store.daily_attempts()
		catalog = _query_catalog(config) if isinstance(config, SafeApplyConfig) else []
		data: dict[str, Any] = {
			"config_state": str(ctx.obj.get("config_state") or "invalid"),
			"logged_in": AuthManager(data_dir, platform="zhipin").check_status() is not None,
			"daily_attempts": daily,
			"daily_remaining": max(0, config.daily_attempt_limit - daily) if isinstance(config, SafeApplyConfig) else None,
			"daily_limit": config.daily_attempt_limit if isinstance(config, SafeApplyConfig) else None,
			"circuit_breaker": store.circuit_breaker(),
			"probe_gate": store.probe_gate(),
			"recovery_gate": store.recovery_gate(),
			"query_schedule": store.query_schedule(config.digest(), catalog) if isinstance(config, SafeApplyConfig) else None,
			"search_cooldown_remaining_seconds": (
				store.search_cooldown_remaining(config.search_interval_seconds)
				if isinstance(config, SafeApplyConfig)
				else None
			),
			"cooldown_remaining_seconds": _cooldown_remaining(store, config) if isinstance(config, SafeApplyConfig) else None,
			"browser_runtime": browser_runtime_status(data_dir, config.browser if isinstance(config, SafeApplyConfig) else None),
			"data_dir": str(data_dir),
		}
		if live and isinstance(config, SafeApplyConfig):
			try:
				with _isolated_runtime(data_dir):
					with _safe_platform(data_dir) as platform:
						response = platform.user_info()
						data["live_ok"] = platform.is_success(response)
						if not data["live_ok"]:
							code = platform.parse_error(response)[0]
							data["live_error"] = code
							store.require_recovery(code)
			except Exception as exc:
				code = _error_code_from_exception(exc)
				data["live_ok"] = False
				data["live_error"] = code
				store.require_recovery(code)
			data["recovery_gate"] = store.recovery_gate()
	_echo_json({"ok": not live or bool(data.get("live_ok")), "data": data})


@cli.command("browser-stop")
@click.pass_context
def browser_stop_cmd(ctx: click.Context) -> None:
	"""精确关闭 boss-safe 登记的专用浏览器 PID 树，保留 profile 数据。"""
	data_dir = _data_dir(ctx)
	started = time.monotonic()
	try:
		result = stop_browser(data_dir)
	except BrowserRuntimeError as exc:
		record_diagnostic(data_dir, stage="browser_shutdown", operation="browser-stop", started=started, error_code=exc.code)
		if exc.code == "BROWSER_PROFILE_NOT_CLEAN":
			with SafeStore(data_dir) as store:
				store.open_circuit_breaker(f"browser-stop: {exc.code}")
		raise click.ClickException(f"{exc.code}: {_redact_message(exc)}") from exc
	record_diagnostic(data_dir, stage="browser_shutdown", operation="browser-stop", started=started, error_code="OK")
	_echo_json({"ok": True, "data": result})


@cli.command("preview")
@click.option("--probe", is_flag=True, default=False, help="只执行一个关键词的一次只读验证，不创建批次")
@click.option("--query", "query_text", default=None, help="探针使用的现有精确安全关键词；仅可与 --probe 同用")
@click.pass_context
def preview_cmd(ctx: click.Context, probe: bool, query_text: str | None) -> None:
	"""只读搜索并生成待人工确认批次；不会发起任何写请求。"""
	data_dir, config = _ctx(ctx)
	with SafeStore(data_dir) as store:
		store.expire_batches()
		if store.circuit_breaker():
			raise click.ClickException("安全熔断已打开；人工核验并 acknowledge-risk 前禁止只读探针和完整预览")
		if store.recovery_gate()["required"]:
			raise click.ClickException("上一轮异常尚未取得新的恢复授权；请先明确授权并执行 acknowledge-risk")
		if query_text is not None and not probe:
			raise click.ClickException("--query 只能与 preview --probe 同用；普通预览必须复用已通过探针的同一关键词")

		catalog = _query_catalog(config)
		if probe:
			# 每次新探针都先撤销旧授权；只有本次成功才能重新放行。
			store.require_probe()
			preferred = _query_by_exact_text(config, query_text) if query_text is not None else None
			query_key = store.choose_query_key(
				config.digest(),
				catalog,
				preferred_query_key=preferred.key if preferred is not None else None,
			)
			if query_key is None:
				if preferred is not None:
					raise click.ClickException(f"关键词 {preferred.query} 今日已完成或已因风险阻断，不得重复搜索")
				raise click.ClickException("NO_ELIGIBLE_QUERY_TODAY: 今日安全关键词均已完成或阻断")
			spec = _query_by_key(config, query_key)
			assert spec is not None
			remaining = store.reserve_probe_search(
				spec.key,
				spec.query,
				_search_interval(config),
			)
			if remaining:
				raise click.ClickException(f"搜索冷却中，还需等待 {remaining} 秒；本次未访问平台，探针仍未通过")
		else:
			gate = store.probe_gate()
			query_key = str(gate.get("query_key") or "")
			spec = _query_by_key(config, query_key)
			if (
				spec is None
				or not gate.get("passed")
				or gate.get("query") != spec.query
			):
				raise click.ClickException("探针门禁未通过、已过期或未绑定当前关键词；请先运行一次成功的 preview --probe")
			begin_status, remaining = store.begin_preview_search(
				config.digest(),
				spec.key,
				spec.query,
				_search_interval(config),
			)
			if begin_status == "cooldown":
				raise click.ClickException(f"搜索冷却中，还需等待 {remaining} 秒；本次未访问平台，探针门禁保持有效")
			if begin_status != "ok":
				raise click.ClickException("探针门禁未通过、已过期或关键词不匹配；请重新运行专属探针")
		try:
			with _isolated_runtime(data_dir):
				with _safe_platform(data_dir) as platform:
					candidates, requests, errors = collect_candidates(
						platform,
						store,
						config,
						max_requests=1,
						query_specs=(spec,),
					)
		except SafeApplyError as exc:
			_record_read_failure(
				store,
				config,
				catalog,
				spec,
				exc.code,
				str(exc),
				open_breaker=exc.stop,
			)
			raise click.ClickException(f"{exc.code}: {_redact_message(exc)}") from exc
		except (Exception, KeyboardInterrupt) as exc:
			code = _error_code_from_exception(exc)
			_record_read_failure(
				store,
				config,
				catalog,
				spec,
				code,
				str(exc),
				open_breaker=code in _READ_ONLY_BREAKER_CODES,
			)
			raise click.ClickException(f"{code}: {_redact_message(exc)}") from exc
		if errors:
			first = errors[0]
			code = first["code"]
			message = first["message"]
			_record_read_failure(
				store,
				config,
				catalog,
				spec,
				code,
				message,
				open_breaker=code in _READ_ONLY_BREAKER_CODES,
			)
			raise click.ClickException(f"{code}: 单关键词只读搜索失败: {message}")
		if probe:
			store.mark_probe_passed(config.digest(), spec.key, spec.query, config.plan_ttl_minutes)
			_echo_json({
				"ok": True,
				"mode": "probe",
				"query_key": spec.key,
				"query": spec.query,
				"search_requests": requests,
				"matched_count": len(candidates),
				"batch_id": None,
				"message": "单次只读探针正常；未创建批次、未保存临时安全 ID、未发送招呼",
				"next": f"完整等待 {_search_interval(config)} 秒搜索冷却后，再单独运行 boss-safe preview",
			})
			return
		if not candidates:
			store.mark_query_completed(config.digest(), catalog, spec.key)
			_echo_json({
				"ok": True,
				"query_key": spec.key,
				"query": spec.query,
				"batch_id": None,
				"count": 0,
				"search_requests": requests,
				"errors": [],
				"items": [],
			})
			return
		batch_id = store.create_batch(candidates, config.digest(), config.plan_ttl_minutes)
		store.mark_query_completed(config.digest(), catalog, spec.key)
		public_items = [
			{key: value for key, value in item.items() if key != "security_id" and key != "job_id"}
			for item in candidates
		]
		_echo_json({
			"ok": True,
			"query_key": spec.key,
			"query": spec.query,
			"batch_id": batch_id,
			"count": len(candidates),
			"search_requests": requests,
			"errors": errors,
			"items": public_items,
			"next": f"人工检查后运行 boss-safe execute {batch_id}",
		})


@cli.command("execute")
@click.argument("batch_id")
@click.pass_context
def execute_cmd(ctx: click.Context, batch_id: str) -> None:
	"""人工确认后执行一个待投递批次。"""
	if not _is_interactive_terminal():
		raise click.ClickException("真实投递只允许交互式终端执行；预览和测试不会发送")
	data_dir, config = _ctx(ctx)
	with SafeStore(data_dir) as store:
		batch = store.get_batch(batch_id)
		if batch is None:
			raise click.ClickException("批次不存在、已过期或已清理")
		if batch["status"] != "PREVIEWED":
			raise click.ClickException(f"批次当前状态为 {batch['status']}，不能重复执行")
		if store.circuit_breaker():
			raise click.ClickException("安全熔断已打开，请先人工处理并执行 acknowledge-risk")
		if store.recovery_gate()["required"]:
			raise click.ClickException("上一轮异常尚未取得新的恢复授权，禁止执行批次")
		remaining = _cooldown_remaining(store, config)
		if remaining:
			raise click.ClickException(f"批次冷却中，还需等待约 {remaining} 秒")
		items = store.batch_items_public(batch_id)
		click.echo(f"批次 {batch_id}，共 {len(items)} 个职位；以下文案将在确认后发送：")
		for item in items:
			click.echo(f"[{item['position']}] {item['title']} | {item['company']} | {item['track_label']}")
			click.echo(f"    {item['message']}")
		excluded = _parse_positions(click.prompt("要排除的编号（逗号分隔，直接回车=全部保留）", default=""), len(items))
		store.exclude_positions(batch_id, excluded)
		confirmation = click.prompt(f"请输入确认短语：确认投递 {batch_id}", default="")
		if confirmation.strip() != f"确认投递 {batch_id}":
			click.echo("未匹配确认短语，批次保留为 PREVIEWED，未发起任何写请求。")
			return
		batch = store.get_batch(batch_id)
		assert batch is not None
		try:
			with _isolated_runtime(data_dir):
				with _safe_platform(data_dir) as platform:
					result = execute_batch_items(store, batch, config, platform)
		except (Exception, KeyboardInterrupt) as exc:
			code = _error_code_from_exception(exc)
			store.finish_batch(batch_id, "STOPPED", code)
			if code in _WRITE_BREAKER_CODES:
				store.open_circuit_breaker(f"batch {batch_id}: {code}")
			store.require_recovery(code)
			raise click.ClickException(f"执行失败: {code}: {_redact_message(exc)}") from exc
		_echo_json({"ok": True, "data": result})


@cli.command("history")
@click.option("--limit", default=20, type=click.IntRange(min=1, max=100), show_default=True)
@click.pass_context
def history_cmd(ctx: click.Context, limit: int) -> None:
	data_dir = _data_dir(ctx)
	with SafeStore(data_dir) as store:
		_echo_json({"ok": True, "items": store.history(limit)})


@cli.command("stop")
@click.option("--reason", default="用户手动停止", show_default=True)
@click.pass_context
def stop_cmd(ctx: click.Context, reason: str) -> None:
	data_dir = _data_dir(ctx)
	with SafeStore(data_dir) as store:
		store.open_circuit_breaker(reason)
	_echo_json({"ok": True, "message": "安全熔断已打开", "reason": reason})


@cli.command("acknowledge-risk")
@click.option("--reason", required=True, help="人工确认已处理风险的说明")
@click.pass_context
def acknowledge_risk_cmd(ctx: click.Context, reason: str) -> None:
	data_dir, _ = _ctx(ctx)
	with SafeStore(data_dir) as store:
		store.acknowledge_risk(reason)
	_echo_json({"ok": True, "message": "已记录人工确认；不会自动恢复或自动执行批次", "reason": reason})


if __name__ == "__main__":
	cli()
