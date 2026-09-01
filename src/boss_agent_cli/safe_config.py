"""可公开复用的求职者安全投递配置。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from platformdirs import user_data_path

from boss_agent_cli.api.endpoints import CITY_CODES


CONFIG_SCHEMA_VERSION = 1
DEFAULT_BATCH_LIMIT = 20
DEFAULT_DAILY_ATTEMPT_LIMIT = 150
DEFAULT_WRITE_DELAY = (15.0, 30.0)
DEFAULT_BATCH_COOLDOWN_MINUTES = 10
DEFAULT_PLAN_TTL_MINUTES = 30
DEFAULT_MAX_SEARCH_REQUESTS = 20
DEFAULT_SEARCH_INTERVAL_SECONDS = 60


class SafeConfigError(ValueError):
	"""安全配置缺失、不兼容或校验失败。"""


class SafeConfigMissingError(SafeConfigError):
	pass


class SafeConfigLegacyError(SafeConfigError):
	pass


@dataclass(frozen=True, slots=True)
class ApplicantProfile:
	school: str
	major: str
	graduation_year: int
	city: str
	job_type: str
	availability: str
	min_internship_months: int

	def identity(self) -> str:
		return f"{self.school}{self.major}{self.graduation_year}届本科生"

	def validate(self) -> None:
		if not 2 <= len(self.school.strip()) <= 40:
			raise SafeConfigError("school 必须为 2-40 个字符")
		if not 2 <= len(self.major.strip()) <= 50:
			raise SafeConfigError("major 必须填写本人专业全称（2-50 个字符）")
		if not 2020 <= self.graduation_year <= 2100:
			raise SafeConfigError("graduation_year 必须为有效四位年份")
		if self.city not in CITY_CODES:
			raise SafeConfigError(f"不支持的城市：{self.city}；请使用 boss cities 列出的城市")
		if self.job_type != "实习":
			raise SafeConfigError("boss-safe 公开版固定用于日常实习，不允许应届或全职类型")
		if not 2 <= len(self.availability.strip()) <= 30:
			raise SafeConfigError("availability 必须为 2-30 个字符")
		if not 1 <= self.min_internship_months <= 24:
			raise SafeConfigError("min_internship_months 必须在 1-24 之间")
		if re.search(r"https?://|www\.|1[3-9]\d{9}", self.availability, flags=re.IGNORECASE):
			raise SafeConfigError("availability 不得包含 URL 或手机号")


@dataclass(frozen=True, slots=True)
class BrowserConfig:
	kind: str = "auto"
	executable: str | None = None
	cdp_port: int = 9222

	def validate(self) -> None:
		if self.kind not in {"auto", "edge", "chrome", "chromium"}:
			raise SafeConfigError("browser.kind 必须是 auto/edge/chrome/chromium")
		if self.executable is not None and not self.executable.strip():
			raise SafeConfigError("browser.executable 为空时必须使用 null")
		if not 1024 <= self.cdp_port <= 65535:
			raise SafeConfigError("browser.cdp_port 必须在 1024-65535 之间")


@dataclass(frozen=True, slots=True)
class SearchTrack:
	id: str
	priority: int
	label: str
	queries: tuple[str, ...]
	match_terms: tuple[str, ...]


SEARCH_TRACKS: tuple[SearchTrack, ...] = (
	SearchTrack("agent", 1, "Agent 应用", ("AI Agent", "智能体开发"),
		("ai agent", "agent开发", "智能体", "多智能体", "rag", "llm应用", "大模型应用", "ai应用", "工作流", "知识库")),
	SearchTrack("llm", 2, "大模型", ("大模型", "LLM应用"),
		("大模型", "llm", "语言模型", "transformers", "微调", "aigc", "生成式")),
	SearchTrack("cv", 3, "CV", ("计算机视觉", "图像算法"),
		("计算机视觉", "视觉", "图像", "医学影像", "目标检测", "图像分割", "opencv", "cv")),
	SearchTrack("fullstack", 4, "全栈", ("AI全栈", "全栈开发"),
		("全栈", "前后端", "ai应用", "web", "rest api", "flask", "django", "fastapi", "node")),
	SearchTrack("frontend", 5, "前端", ("前端开发", "Web前端"),
		("前端", "web", "react", "vue", "typescript", "javascript")),
	SearchTrack("research", 6, "科研/科学计算", ("科研助理", "科学计算"),
		("科研", "科学计算", "数值模拟", "并行计算", "高性能", "仿真", "cuda", "mpi", "fftw", "材料计算")),
	SearchTrack("algorithm", 7, "算法", ("算法实习生", "机器学习"),
		("算法", "机器学习", "深度学习", "数据挖掘", "推荐", "nlp", "自然语言", "模型")),
	SearchTrack("backend", 8, "后端", ("Python后端", "后端开发"),
		("后端", "服务端", "python", "java", "c++", "api", "flask", "django", "fastapi", "spring", "go", "微服务")),
	SearchTrack("test", 9, "测试开发", ("测试开发", "自动化测试"),
		("测试开发", "自动化测试", "测试工程", "质量", "qa", "sdet", "pytest", "selenium", "playwright")),
	SearchTrack("other", 10, "其他技术开发", ("软件开发实习生", "研发实习生"),
		("软件开发", "研发工程师", "开发工程师", "程序", "python", "java", "c++", "后端", "前端", "算法", "测试开发")),
)

_TRACK_BY_ID = {track.id: track for track in SEARCH_TRACKS}


@dataclass(frozen=True, slots=True)
class SearchProfile:
	id: str
	priority: int
	label: str
	queries: tuple[str, ...]
	claim: str
	match_terms: tuple[str, ...]
	resume_evidence: tuple[str, ...]

	def render_message(self, applicant: ApplicantProfile) -> str:
		return (
			f"HR您好，我是{applicant.identity()}，{self.claim}，"
			f"{applicant.availability}，可连续实习{applicant.min_internship_months}个月以上，期待沟通！"
		)

	def as_config_dict(self) -> dict[str, Any]:
		return {"id": self.id, "claim": self.claim, "resume_evidence": list(self.resume_evidence)}


def make_search_profile(track_id: str, claim: str, resume_evidence: tuple[str, ...]) -> SearchProfile:
	try:
		track = _TRACK_BY_ID[track_id]
	except KeyError as exc:
		raise SafeConfigError(f"未知技术方向：{track_id}") from exc
	return SearchProfile(
		id=track.id,
		priority=track.priority,
		label=track.label,
		queries=track.queries,
		claim=claim.strip(),
		match_terms=track.match_terms,
		resume_evidence=tuple(item.strip() for item in resume_evidence if item.strip()),
	)


NON_TECH_TERMS: tuple[str, ...] = (
	"销售", "客服", "招聘", "人事", "招生", "地推", "主播", "直播", "带货", "电话销售",
	"商务拓展", "渠道拓展", "客户成功", "课程顾问", "招生顾问", "猎头",
	"运营", "新媒体", "社群", "产品经理", "产品助理", "ui设计", "视觉设计", "平面设计",
	"数据标注", "标注员", "内容审核", "技术支持", "实施工程师", "售前", "售后", "运维工程师",
	"外包", "驻场", "外派", "劳务", "派遣", "第三方用工", "物业", "门店", "配送", "司机",
)

MESSAGE_BLACKLIST: tuple[str, ...] = (
	"Traceback", "Error", "抱歉，作为", "I cannot", "I apologize", "I'm an AI", "```",
)


@dataclass(frozen=True, slots=True)
class SafeApplyConfig:
	applicant: ApplicantProfile
	profiles: tuple[SearchProfile, ...]
	browser: BrowserConfig = BrowserConfig()
	batch_limit: int = DEFAULT_BATCH_LIMIT
	daily_attempt_limit: int = DEFAULT_DAILY_ATTEMPT_LIMIT
	write_delay: tuple[float, float] = DEFAULT_WRITE_DELAY
	batch_cooldown_minutes: int = DEFAULT_BATCH_COOLDOWN_MINUTES
	plan_ttl_minutes: int = DEFAULT_PLAN_TTL_MINUTES
	max_search_requests: int = DEFAULT_MAX_SEARCH_REQUESTS
	company_per_batch: int = 1
	company_per_day: int = DEFAULT_DAILY_ATTEMPT_LIMIT
	search_interval_seconds: int = DEFAULT_SEARCH_INTERVAL_SECONDS

	@property
	def city(self) -> str:
		return self.applicant.city

	@property
	def job_type(self) -> str:
		return self.applicant.job_type

	def validate(self) -> None:
		self.applicant.validate()
		self.browser.validate()
		if not self.profiles:
			raise SafeConfigError("至少启用一个技术方向")
		ids = [profile.id for profile in self.profiles]
		if len(ids) != len(set(ids)):
			raise SafeConfigError("技术方向 ID 不得重复")
		for profile in self.profiles:
			if profile.id not in _TRACK_BY_ID:
				raise SafeConfigError(f"未知技术方向：{profile.id}")
			if not 5 <= len(profile.claim) <= 80:
				raise SafeConfigError(f"{profile.id}.claim 必须为 5-80 个字符")
			if not profile.resume_evidence:
				raise SafeConfigError(f"{profile.id}.resume_evidence 至少填写一条")
			if any(not 2 <= len(item) <= 120 for item in profile.resume_evidence):
				raise SafeConfigError(f"{profile.id}.resume_evidence 每条必须为 2-120 个字符")
			reasons = validate_message(profile.render_message(self.applicant))
			if reasons:
				raise SafeConfigError(f"{profile.id} 招呼语无效：{'；'.join(reasons)}")
		if not 1 <= self.batch_limit <= DEFAULT_BATCH_LIMIT:
			raise SafeConfigError(f"batch_limit 必须在 1-{DEFAULT_BATCH_LIMIT} 之间")
		if not 1 <= self.daily_attempt_limit <= DEFAULT_DAILY_ATTEMPT_LIMIT:
			raise SafeConfigError(f"daily_attempt_limit 必须在 1-{DEFAULT_DAILY_ATTEMPT_LIMIT} 之间")
		if len(self.write_delay) != 2 or self.write_delay[0] < 15 or self.write_delay[0] > self.write_delay[1]:
			raise SafeConfigError("write_delay 最小值不得低于 15 秒，且必须是合法范围")
		if self.batch_cooldown_minutes < 10:
			raise SafeConfigError("batch_cooldown_minutes 不得低于 10 分钟")
		if not 1 <= self.plan_ttl_minutes <= 30:
			raise SafeConfigError("plan_ttl_minutes 必须在 1-30 分钟之间")
		if not 1 <= self.max_search_requests <= DEFAULT_MAX_SEARCH_REQUESTS:
			raise SafeConfigError(f"max_search_requests 必须在 1-{DEFAULT_MAX_SEARCH_REQUESTS} 之间")
		if self.company_per_batch != 1:
			raise SafeConfigError("安全投递固定为同批同公司 1 个")
		if not 1 <= self.company_per_day <= self.daily_attempt_limit:
			raise SafeConfigError("company_per_day 必须在 1 到每日写请求硬上限之间")
		if self.search_interval_seconds < DEFAULT_SEARCH_INTERVAL_SECONDS:
			raise SafeConfigError(f"search_interval_seconds 不得低于 {DEFAULT_SEARCH_INTERVAL_SECONDS}")

	def as_dict(self) -> dict[str, Any]:
		return {
			"schema_version": CONFIG_SCHEMA_VERSION,
			"applicant": asdict(self.applicant),
			"tracks": [profile.as_config_dict() for profile in self.profiles],
			"limits": {
				"batch_limit": self.batch_limit,
				"daily_attempt_limit": self.daily_attempt_limit,
				"write_delay": list(self.write_delay),
				"batch_cooldown_minutes": self.batch_cooldown_minutes,
				"plan_ttl_minutes": self.plan_ttl_minutes,
				"max_search_requests": self.max_search_requests,
				"company_per_batch": self.company_per_batch,
				"company_per_day": self.company_per_day,
				"search_interval_seconds": self.search_interval_seconds,
			},
			"browser": asdict(self.browser),
		}

	def digest(self) -> str:
		raw = json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
		return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def default_data_dir() -> Path:
	return Path(user_data_path("boss-agent-safe", appauthor=False, roaming=False))


def config_path(data_dir: Path) -> Path:
	return data_dir / "safe-config.json"


def read_config_document(data_dir: Path) -> dict[str, Any] | None:
	path = config_path(data_dir)
	if not path.exists():
		return None
	try:
		loaded = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError) as exc:
		raise SafeConfigError(f"安全配置无法读取：{path}: {exc}") from exc
	if not isinstance(loaded, dict):
		raise SafeConfigError("安全配置根节点必须是 JSON 对象")
	return loaded


def config_state(data_dir: Path) -> str:
	try:
		raw = read_config_document(data_dir)
	except SafeConfigError:
		return "invalid"
	if raw is None:
		return "missing"
	if raw.get("schema_version") != CONFIG_SCHEMA_VERSION:
		return "legacy"
	try:
		_decode_config(raw)
	except SafeConfigError:
		return "invalid"
	return "ready"


def legacy_limit_defaults(raw: Mapping[str, Any] | None) -> dict[str, Any]:
	raw = raw or {}
	return {
		"batch_limit": int(raw.get("batch_limit", DEFAULT_BATCH_LIMIT)),
		"daily_attempt_limit": int(raw.get("daily_attempt_limit", DEFAULT_DAILY_ATTEMPT_LIMIT)),
		"write_delay": tuple(float(value) for value in raw.get("write_delay", DEFAULT_WRITE_DELAY)),
		"batch_cooldown_minutes": int(raw.get("batch_cooldown_minutes", DEFAULT_BATCH_COOLDOWN_MINUTES)),
		"plan_ttl_minutes": int(raw.get("plan_ttl_minutes", DEFAULT_PLAN_TTL_MINUTES)),
		"max_search_requests": int(raw.get("max_search_requests", DEFAULT_MAX_SEARCH_REQUESTS)),
		"company_per_batch": int(raw.get("company_per_batch", 1)),
		"company_per_day": int(raw.get("company_per_day", raw.get("daily_attempt_limit", DEFAULT_DAILY_ATTEMPT_LIMIT))),
		"search_interval_seconds": int(raw.get("search_interval_seconds", DEFAULT_SEARCH_INTERVAL_SECONDS)),
	}


def _decode_config(raw: Mapping[str, Any]) -> SafeApplyConfig:
	if raw.get("schema_version") != CONFIG_SCHEMA_VERSION:
		raise SafeConfigLegacyError("检测到旧版配置；请运行 boss-safe init 交互迁移")
	applicant_raw = raw.get("applicant")
	tracks_raw = raw.get("tracks")
	limits = raw.get("limits", {})
	browser_raw = raw.get("browser", {})
	if not isinstance(applicant_raw, Mapping) or not isinstance(tracks_raw, list):
		raise SafeConfigError("配置缺少 applicant 或 tracks")
	if not isinstance(limits, Mapping) or not isinstance(browser_raw, Mapping):
		raise SafeConfigError("limits/browser 必须是 JSON 对象")
	applicant = ApplicantProfile(
		school=str(applicant_raw.get("school", "")),
		major=str(applicant_raw.get("major", "")),
		graduation_year=int(applicant_raw.get("graduation_year", 0)),
		city=str(applicant_raw.get("city", "")),
		job_type=str(applicant_raw.get("job_type", "")),
		availability=str(applicant_raw.get("availability", "")),
		min_internship_months=int(applicant_raw.get("min_internship_months", 0)),
	)
	profiles: list[SearchProfile] = []
	for item in tracks_raw:
		if not isinstance(item, Mapping):
			raise SafeConfigError("tracks 中每项必须是 JSON 对象")
		evidence = item.get("resume_evidence", [])
		if not isinstance(evidence, list):
			raise SafeConfigError("resume_evidence 必须是数组")
		profiles.append(make_search_profile(str(item.get("id", "")), str(item.get("claim", "")), tuple(map(str, evidence))))
	raw_delay = limits.get("write_delay", DEFAULT_WRITE_DELAY)
	if not isinstance(raw_delay, (list, tuple)) or len(raw_delay) != 2:
		raise SafeConfigError("write_delay 必须是包含两个数字的数组")
	daily_limit = int(limits.get("daily_attempt_limit", DEFAULT_DAILY_ATTEMPT_LIMIT))
	config = SafeApplyConfig(
		applicant=applicant,
		profiles=tuple(profiles),
		browser=BrowserConfig(
			kind=str(browser_raw.get("kind", "auto")),
			executable=(str(browser_raw["executable"]) if browser_raw.get("executable") is not None else None),
			cdp_port=int(browser_raw.get("cdp_port", 9222)),
		),
		batch_limit=int(limits.get("batch_limit", DEFAULT_BATCH_LIMIT)),
		daily_attempt_limit=daily_limit,
		write_delay=(float(raw_delay[0]), float(raw_delay[1])),
		batch_cooldown_minutes=int(limits.get("batch_cooldown_minutes", DEFAULT_BATCH_COOLDOWN_MINUTES)),
		plan_ttl_minutes=int(limits.get("plan_ttl_minutes", DEFAULT_PLAN_TTL_MINUTES)),
		max_search_requests=int(limits.get("max_search_requests", DEFAULT_MAX_SEARCH_REQUESTS)),
		company_per_batch=int(limits.get("company_per_batch", 1)),
		company_per_day=int(limits.get("company_per_day", daily_limit)),
		search_interval_seconds=int(limits.get("search_interval_seconds", DEFAULT_SEARCH_INTERVAL_SECONDS)),
	)
	config.validate()
	return config


def load_config(data_dir: Path) -> SafeApplyConfig:
	raw = read_config_document(data_dir)
	if raw is None:
		raise SafeConfigMissingError("尚未初始化；请先运行 boss-safe init")
	return _decode_config(raw)


def save_config(data_dir: Path, config: SafeApplyConfig, *, backup_existing: bool = True) -> Path | None:
	config.validate()
	data_dir.mkdir(parents=True, exist_ok=True)
	path = config_path(data_dir)
	backup: Path | None = None
	if backup_existing and path.exists():
		stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
		backup = path.with_name(f"safe-config.backup-{stamp}.json")
		counter = 1
		while backup.exists():
			backup = path.with_name(f"safe-config.backup-{stamp}-{counter}.json")
			counter += 1
		shutil.copy2(path, backup)
	temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
	try:
		temporary.write_text(json.dumps(config.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
		if os.name != "nt":
			os.chmod(temporary, 0o600)
		os.replace(temporary, path)
	finally:
		temporary.unlink(missing_ok=True)
	return backup


def normalize_text(*parts: Any) -> str:
	return " ".join(str(part or "") for part in parts).strip().lower()


def company_key(company: str) -> str:
	return re.sub(r"\s+", "", company).lower()


def validate_message(message: str) -> list[str]:
	reasons: list[str] = []
	if not 30 <= len(message) <= 120:
		reasons.append(f"长度不在 30-120 字范围内: {len(message)}")
	if not re.search(r"[一-鿿]", message):
		reasons.append("缺少中文字符")
	if "{" in message or "}" in message or "<" in message or ">" in message:
		reasons.append("包含未替换变量")
	if re.search(r"https?://|www\.", message, flags=re.IGNORECASE):
		reasons.append("包含 URL")
	if re.search(r"1[3-9]\d{9}", message):
		reasons.append("包含手机号")
	for term in MESSAGE_BLACKLIST:
		if term in message:
			reasons.append(f"包含禁止文本: {term}")
			break
	return reasons


def profile_by_id(config: SafeApplyConfig, profile_id: str) -> SearchProfile:
	for profile in config.profiles:
		if profile.id == profile_id:
			return profile
	raise KeyError(profile_id)


__all__ = [
	"ApplicantProfile", "BrowserConfig", "CONFIG_SCHEMA_VERSION", "DEFAULT_BATCH_LIMIT",
	"DEFAULT_BATCH_COOLDOWN_MINUTES", "DEFAULT_DAILY_ATTEMPT_LIMIT", "DEFAULT_MAX_SEARCH_REQUESTS",
	"DEFAULT_PLAN_TTL_MINUTES", "DEFAULT_SEARCH_INTERVAL_SECONDS", "DEFAULT_WRITE_DELAY",
	"MESSAGE_BLACKLIST", "NON_TECH_TERMS", "SEARCH_TRACKS", "SafeApplyConfig", "SafeConfigError",
	"SafeConfigLegacyError", "SafeConfigMissingError", "SearchProfile", "SearchTrack", "company_key",
	"config_path", "config_state", "default_data_dir", "legacy_limit_defaults", "load_config",
	"make_search_profile", "normalize_text", "profile_by_id", "read_config_document", "save_config",
	"validate_message",
]
