"""安全投递的本地额度、批次和审计存储。

职位稳定 ID 用于去重；短期 security_id 使用 TokenStore 的机器绑定密钥加密，
只在待执行批次中保留，批次结束或过期后清除。
"""

from __future__ import annotations

import json
import math
import secrets
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from boss_agent_cli.auth.token_store import TokenStore


SHANGHAI = ZoneInfo("Asia/Shanghai")


def now_ts() -> float:
	return time.time()


def today_key(timestamp: float | None = None) -> str:
	return datetime.fromtimestamp(timestamp or now_ts(), SHANGHAI).date().isoformat()


class SafeStore:
	def __init__(self, data_dir: Path) -> None:
		self.data_dir = data_dir
		self.data_dir.mkdir(parents=True, exist_ok=True)
		self._conn = sqlite3.connect(str(data_dir / "safe_apply.db"))
		self._conn.row_factory = sqlite3.Row
		self._cipher = TokenStore(data_dir / "auth")
		self._init_tables()

	def _init_tables(self) -> None:
		self._conn.executescript(
			"""
			CREATE TABLE IF NOT EXISTS batches (
				batch_id TEXT PRIMARY KEY,
				created_at REAL NOT NULL,
				expires_at REAL NOT NULL,
				status TEXT NOT NULL,
				config_digest TEXT NOT NULL,
				stopped_reason TEXT NOT NULL DEFAULT '',
				completed_at REAL
			);
			CREATE TABLE IF NOT EXISTS batch_items (
				batch_id TEXT NOT NULL,
				position INTEGER NOT NULL,
				job_id TEXT NOT NULL,
				security_id_cipher TEXT NOT NULL,
				title TEXT NOT NULL,
				company TEXT NOT NULL,
				city TEXT NOT NULL,
				district TEXT NOT NULL,
				salary TEXT NOT NULL,
				track_id TEXT NOT NULL,
				track_label TEXT NOT NULL,
				query TEXT NOT NULL,
				message TEXT NOT NULL,
				status TEXT NOT NULL DEFAULT 'pending',
				outcome TEXT NOT NULL DEFAULT '',
				error_code TEXT NOT NULL DEFAULT '',
				attempt_id INTEGER,
				attempted_at REAL,
				PRIMARY KEY (batch_id, position),
				UNIQUE (batch_id, job_id)
			);
			CREATE TABLE IF NOT EXISTS write_attempts (
				attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
				day_key TEXT NOT NULL,
				batch_id TEXT NOT NULL,
				position INTEGER NOT NULL,
				job_id TEXT NOT NULL,
				company_key TEXT NOT NULL,
				attempted_at REAL NOT NULL,
				outcome TEXT NOT NULL DEFAULT 'reserved',
				error_code TEXT NOT NULL DEFAULT ''
			);
			CREATE INDEX IF NOT EXISTS write_attempts_day ON write_attempts(day_key);
			CREATE INDEX IF NOT EXISTS write_attempts_job ON write_attempts(job_id);
			CREATE TABLE IF NOT EXISTS control_state (
				key TEXT PRIMARY KEY,
				value TEXT NOT NULL
			);
			"""
		)
		self._conn.commit()

	def close(self) -> None:
		self._conn.close()

	def __enter__(self) -> "SafeStore":
		return self

	def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
		self.close()

	def _set_state(self, key: str, value: Any) -> None:
		self._write_state(key, value)
		self._conn.commit()

	def _write_state(self, key: str, value: Any) -> None:
		self._conn.execute(
			"INSERT OR REPLACE INTO control_state (key, value) VALUES (?, ?)",
			(key, json.dumps(value, ensure_ascii=False, sort_keys=True)),
		)

	def _get_state(self, key: str, default: Any = None) -> Any:
		row = self._conn.execute("SELECT value FROM control_state WHERE key = ?", (key,)).fetchone()
		if row is None:
			return default
		try:
			return json.loads(row[0])
		except json.JSONDecodeError:
			return default

	def circuit_breaker(self) -> dict[str, Any] | None:
		value = self._get_state("circuit_breaker")
		return value if isinstance(value, dict) and value.get("open") else None

	def require_probe(self, *, timestamp: float | None = None) -> None:
		now = timestamp if timestamp is not None else now_ts()
		self._set_state(
			"probe_gate",
			{"required": True, "passed": False, "required_at": now},
		)

	def mark_probe_passed(
		self,
		config_digest: str,
		query_key: str,
		query: str,
		ttl_minutes: int,
		*,
		timestamp: float | None = None,
	) -> None:
		now = timestamp if timestamp is not None else now_ts()
		self._set_state(
			"probe_gate",
			{
				"required": True,
				"passed": True,
				"config_digest": config_digest,
				"query_key": query_key,
				"query": query,
				"passed_at": now,
				"expires_at": now + ttl_minutes * 60,
			},
		)

	def consume_probe_pass(
		self,
		config_digest: str,
		query_key: str,
		query: str,
		*,
		timestamp: float | None = None,
	) -> bool:
		"""消费一次与配置、关键词完全绑定的有效探针。"""
		value = self._get_state("probe_gate")
		if not isinstance(value, dict) or not value.get("required"):
			return False
		now = timestamp if timestamp is not None else now_ts()
		try:
			expires_at = float(value.get("expires_at") or 0)
		except (TypeError, ValueError):
			expires_at = 0
		if (
			not value.get("passed")
			or value.get("config_digest") != config_digest
			or value.get("query_key") != query_key
			or value.get("query") != query
			or expires_at <= now
		):
			return False
		self._set_state(
			"probe_gate",
			{
				"required": True,
				"passed": False,
				"query_key": query_key,
				"query": query,
				"consumed_at": now,
			},
		)
		return True

	def probe_gate(self, *, timestamp: float | None = None) -> dict[str, Any]:
		value = self._get_state("probe_gate")
		if not isinstance(value, dict):
			return {
				"required": False,
				"passed": False,
				"expires_at": None,
				"query_key": None,
				"query": None,
				"consumed_at": None,
			}
		now = timestamp if timestamp is not None else now_ts()
		try:
			expires_at = float(value.get("expires_at") or 0)
		except (TypeError, ValueError):
			expires_at = 0
		passed = bool(value.get("passed")) and expires_at > now
		return {
			"required": bool(value.get("required")),
			"passed": passed,
			"expires_at": expires_at if passed else None,
			"query_key": value.get("query_key"),
			"query": value.get("query"),
			"consumed_at": value.get("consumed_at"),
		}

	def recovery_gate(self) -> dict[str, Any]:
		value = self._get_state("recovery_gate")
		if not isinstance(value, dict) or not value.get("required"):
			return {"required": False, "code": None, "query_key": None, "query": None}
		return {
			"required": True,
			"code": value.get("code"),
			"query_key": value.get("query_key"),
			"query": value.get("query"),
			"stopped_at": value.get("stopped_at"),
		}

	def require_recovery(
		self,
		code: str,
		*,
		query_key: str | None = None,
		query: str | None = None,
		timestamp: float | None = None,
	) -> None:
		now = timestamp if timestamp is not None else now_ts()
		self._set_state(
			"recovery_gate",
			{
				"required": True,
				"code": code,
				"query_key": query_key,
				"query": query,
				"stopped_at": now,
			},
		)
		self.require_probe(timestamp=now)

	def _search_cooldown_remaining(self, now: float, minimum_interval_seconds: int) -> int:
		value = self._get_state("last_search")
		if not isinstance(value, dict):
			return 0
		try:
			last_started_at = float(value.get("started_at") or 0)
		except (TypeError, ValueError):
			return 0
		return max(0, math.ceil(last_started_at + minimum_interval_seconds - now))

	def search_cooldown_remaining(
		self,
		minimum_interval_seconds: int,
		*,
		timestamp: float | None = None,
	) -> int:
		now = timestamp if timestamp is not None else now_ts()
		return self._search_cooldown_remaining(now, minimum_interval_seconds)

	def reserve_probe_search(
		self,
		query_key: str,
		query: str,
		minimum_interval_seconds: int,
		*,
		timestamp: float | None = None,
	) -> int:
		"""原子预留一次探针搜索；返回仍需等待的秒数，0 表示预留成功。"""
		now = timestamp if timestamp is not None else now_ts()
		self._conn.execute("BEGIN IMMEDIATE")
		try:
			remaining = self._search_cooldown_remaining(now, minimum_interval_seconds)
			if remaining:
				self._conn.rollback()
				return remaining
			self._write_state(
				"last_search",
				{"started_at": now, "mode": "probe", "query_key": query_key, "query": query},
			)
			self._conn.commit()
		except BaseException:
			self._conn.rollback()
			raise
		return 0

	def begin_preview_search(
		self,
		config_digest: str,
		query_key: str,
		query: str,
		minimum_interval_seconds: int,
		*,
		timestamp: float | None = None,
	) -> tuple[str, int]:
		"""原子校验并消费关键词探针，同时预留普通预览搜索。"""
		now = timestamp if timestamp is not None else now_ts()
		self._conn.execute("BEGIN IMMEDIATE")
		try:
			gate = self._get_state("probe_gate")
			try:
				expires_at = float(gate.get("expires_at") or 0) if isinstance(gate, dict) else 0
			except (TypeError, ValueError):
				expires_at = 0
			if (
				not isinstance(gate, dict)
				or not gate.get("required")
				or not gate.get("passed")
				or gate.get("config_digest") != config_digest
				or gate.get("query_key") != query_key
				or gate.get("query") != query
				or expires_at <= now
			):
				self._conn.rollback()
				return "probe_required", 0
			remaining = self._search_cooldown_remaining(now, minimum_interval_seconds)
			if remaining:
				self._conn.rollback()
				return "cooldown", remaining
			self._write_state(
				"probe_gate",
				{
					"required": True,
					"passed": False,
					"query_key": query_key,
					"query": query,
					"consumed_at": now,
				},
			)
			self._write_state(
				"last_search",
				{"started_at": now, "mode": "preview", "query_key": query_key, "query": query},
			)
			self._conn.commit()
		except BaseException:
			self._conn.rollback()
			raise
		return "ok", 0

	def _normalized_query_schedule(
		self,
		config_digest: str,
		catalog: list[dict[str, str]],
		*,
		timestamp: float | None = None,
	) -> dict[str, Any]:
		now = timestamp if timestamp is not None else now_ts()
		day = today_key(now)
		keys = [item["key"] for item in catalog]
		value = self._get_state("query_schedule")
		if not isinstance(value, dict) or value.get("config_digest") != config_digest:
			return {"config_digest": config_digest, "day_key": day, "cursor": 0, "completed": [], "blocked": {}}
		try:
			cursor = int(value.get("cursor") or 0)
		except (TypeError, ValueError):
			cursor = 0
		completed = value.get("completed") if value.get("day_key") == day else []
		blocked = value.get("blocked") if value.get("day_key") == day else {}
		if not isinstance(completed, list):
			completed = []
		if not isinstance(blocked, dict):
			blocked = {}
		return {
			"config_digest": config_digest,
			"day_key": day,
			"cursor": cursor % max(1, len(catalog)),
			"completed": [key for key in completed if key in keys],
			"blocked": {key: item for key, item in blocked.items() if key in keys and isinstance(item, dict)},
		}

	@staticmethod
	def _next_query_key(state: dict[str, Any], catalog: list[dict[str, str]]) -> str | None:
		if not catalog:
			return None
		unavailable = set(state["completed"]) | set(state["blocked"])
		cursor = int(state["cursor"])
		for offset in range(len(catalog)):
			key = catalog[(cursor + offset) % len(catalog)]["key"]
			if key not in unavailable:
				return key
		return None

	def query_schedule(
		self,
		config_digest: str,
		catalog: list[dict[str, str]],
		*,
		timestamp: float | None = None,
	) -> dict[str, Any]:
		state = self._normalized_query_schedule(config_digest, catalog, timestamp=timestamp)
		self._set_state("query_schedule", state)
		by_key = {item["key"]: item for item in catalog}
		next_key = self._next_query_key(state, catalog)
		blocked = []
		for key, value in state["blocked"].items():
			blocked.append({"query_key": key, "query": by_key[key]["query"], **value})
		return {
			"day_key": state["day_key"],
			"cursor": state["cursor"],
			"completed_today": list(state["completed"]),
			"blocked_today": blocked,
			"next_query_key": next_key,
			"next_query": by_key[next_key]["query"] if next_key is not None else None,
		}

	def choose_query_key(
		self,
		config_digest: str,
		catalog: list[dict[str, str]],
		*,
		preferred_query_key: str | None = None,
		timestamp: float | None = None,
	) -> str | None:
		state = self._normalized_query_schedule(config_digest, catalog, timestamp=timestamp)
		self._set_state("query_schedule", state)
		if preferred_query_key is not None:
			unavailable = set(state["completed"]) | set(state["blocked"])
			return preferred_query_key if preferred_query_key not in unavailable else None
		return self._next_query_key(state, catalog)

	def _advance_query(
		self,
		config_digest: str,
		catalog: list[dict[str, str]],
		query_key: str,
		*,
		blocked: dict[str, Any] | None = None,
		timestamp: float | None = None,
	) -> None:
		state = self._normalized_query_schedule(config_digest, catalog, timestamp=timestamp)
		keys = [item["key"] for item in catalog]
		if query_key not in keys:
			raise ValueError(f"未知查询键: {query_key}")
		if blocked is None:
			if query_key not in state["completed"]:
				state["completed"].append(query_key)
		else:
			state["blocked"][query_key] = blocked
		state["cursor"] = (keys.index(query_key) + 1) % len(keys)
		self._set_state("query_schedule", state)

	def mark_query_completed(
		self,
		config_digest: str,
		catalog: list[dict[str, str]],
		query_key: str,
		*,
		timestamp: float | None = None,
	) -> None:
		self._advance_query(config_digest, catalog, query_key, timestamp=timestamp)

	def block_query(
		self,
		config_digest: str,
		catalog: list[dict[str, str]],
		query_key: str,
		query: str,
		code: str,
		*,
		timestamp: float | None = None,
	) -> None:
		now = timestamp if timestamp is not None else now_ts()
		self._advance_query(
			config_digest,
			catalog,
			query_key,
			blocked={"query": query, "code": code, "blocked_at": now},
			timestamp=now,
		)

	def open_circuit_breaker(self, reason: str) -> None:
		self._set_state("circuit_breaker", {"open": True, "reason": reason, "opened_at": now_ts()})
		self.require_probe()
		# 熔断后不允许旧批次继续携带可写的临时安全标识；职位展示字段和
		# 审计记录保留，只有密文 security_id 被清除。
		self._conn.execute(
			"""UPDATE batch_items SET security_id_cipher = ''
			WHERE batch_id IN (SELECT batch_id FROM batches WHERE status IN ('PREVIEWED', 'EXECUTING'))""",
		)
		self._conn.commit()

	def acknowledge_risk(self, reason: str) -> None:
		now = now_ts()
		with self._conn:
			self._write_state("circuit_breaker", {"open": False, "reason": reason, "acknowledged_at": now})
			self._write_state("recovery_gate", {"required": False, "acknowledged_at": now})
			self._write_state("probe_gate", {"required": True, "passed": False, "required_at": now})

	def expire_batches(self, timestamp: float | None = None) -> int:
		now = timestamp or now_ts()
		rows = self._conn.execute(
			"SELECT batch_id FROM batches WHERE status IN ('PREVIEWED', 'EXECUTING') AND expires_at < ?",
			(now,),
		).fetchall()
		for row in rows:
			self._conn.execute(
				"UPDATE batches SET status = 'EXPIRED', stopped_reason = 'plan_expired' WHERE batch_id = ?",
				(row[0],),
			)
			self._conn.execute(
				"UPDATE batch_items SET security_id_cipher = '' WHERE batch_id = ?",
				(row[0],),
			)
		self._conn.commit()
		return len(rows)

	def has_attempted_job(self, job_id: str) -> bool:
		row = self._conn.execute("SELECT 1 FROM write_attempts WHERE job_id = ? LIMIT 1", (job_id,)).fetchone()
		return row is not None

	def daily_attempts(self, day_key: str | None = None) -> int:
		row = self._conn.execute(
			"SELECT COUNT(*) FROM write_attempts WHERE day_key = ?",
			(day_key or today_key(),),
		).fetchone()
		return int(row[0])

	def daily_company_attempts(self, company_key: str, day_key: str | None = None) -> int:
		row = self._conn.execute(
			"SELECT COUNT(*) FROM write_attempts WHERE day_key = ? AND company_key = ?",
			(day_key or today_key(), company_key),
		).fetchone()
		return int(row[0])

	def batch_attempts(self, batch_id: str) -> int:
		row = self._conn.execute("SELECT COUNT(*) FROM write_attempts WHERE batch_id = ?", (batch_id,)).fetchone()
		return int(row[0])

	def last_completed_at(self) -> float | None:
		row = self._conn.execute(
			"SELECT completed_at FROM batches WHERE completed_at IS NOT NULL ORDER BY completed_at DESC LIMIT 1"
		).fetchone()
		return float(row[0]) if row else None

	def create_batch(self, items: list[dict[str, Any]], config_digest: str, ttl_minutes: int) -> str:
		batch_id = f"safe-{datetime.now(SHANGHAI).strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
		created = now_ts()
		with self._conn:
			self._conn.execute(
				"INSERT INTO batches (batch_id, created_at, expires_at, status, config_digest) VALUES (?, ?, ?, 'PREVIEWED', ?)",
				(batch_id, created, created + ttl_minutes * 60, config_digest),
			)
			for position, item in enumerate(items, start=1):
				cipher = self._cipher._encrypt_text(str(item["security_id"]))  # noqa: SLF001 - 同包安全存储内部接口
				self._conn.execute(
					"""INSERT INTO batch_items
					(batch_id, position, job_id, security_id_cipher, title, company, city, district, salary,
					 track_id, track_label, query, message)
					VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
					(
						batch_id, position, item["job_id"], cipher, item["title"], item["company"],
						item["city"], item["district"], item["salary"], item["track_id"],
						item["track_label"], item["query"], item["message"],
					),
				)
		return batch_id

	def get_batch(self, batch_id: str) -> dict[str, Any] | None:
		self.expire_batches()
		batch = self._conn.execute("SELECT * FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
		if batch is None:
			return None
		items = []
		for row in self._conn.execute(
			"SELECT * FROM batch_items WHERE batch_id = ? ORDER BY position", (batch_id,)
		).fetchall():
			item = dict(row)
			item["security_id"] = self._cipher._decrypt_text(  # noqa: SLF001 - 同包安全存储内部接口
				item.pop("security_id_cipher")
			)
			items.append(item)
		result = dict(batch)
		result["items"] = items
		return result

	def exclude_positions(self, batch_id: str, positions: list[int]) -> None:
		for position in positions:
			self._conn.execute(
				"UPDATE batch_items SET status = 'excluded', outcome = 'excluded_by_user' WHERE batch_id = ? AND position = ? AND status = 'pending'",
				(batch_id, position),
			)
		self._conn.commit()

	def begin_batch(self, batch_id: str) -> None:
		self._conn.execute("UPDATE batches SET status = 'EXECUTING' WHERE batch_id = ? AND status = 'PREVIEWED'", (batch_id,))
		self._conn.commit()

	def reserve_attempt(
		self,
		batch_id: str,
		position: int,
		job_id: str,
		company_key: str,
		*,
		daily_limit: int,
		batch_limit: int,
		company_limit: int,
	) -> tuple[bool, str, int | None]:
		day = today_key()
		with self._conn:
			if self.daily_attempts(day) >= daily_limit:
				return False, "DAILY_LIMIT", None
			if self.batch_attempts(batch_id) >= batch_limit:
				return False, "BATCH_LIMIT", None
			if self.daily_company_attempts(company_key, day) >= company_limit:
				return False, "COMPANY_LIMIT", None
			row = self._conn.execute(
				"SELECT status FROM batch_items WHERE batch_id = ? AND position = ? AND job_id = ?",
				(batch_id, position, job_id),
			).fetchone()
			if row is None or row[0] != "pending":
				return False, "ITEM_NOT_PENDING", None
			attempted = now_ts()
			cursor = self._conn.execute(
				"INSERT INTO write_attempts (day_key, batch_id, position, job_id, company_key, attempted_at) VALUES (?, ?, ?, ?, ?, ?)",
				(day, batch_id, position, job_id, company_key, attempted),
			)
			if cursor.lastrowid is None:
				raise RuntimeError("写入审计记录未生成 attempt_id")
			attempt_id = int(cursor.lastrowid)
			self._conn.execute(
				"UPDATE batch_items SET status = 'attempting', attempt_id = ?, attempted_at = ? WHERE batch_id = ? AND position = ?",
				(attempt_id, attempted, batch_id, position),
			)
		return True, "RESERVED", attempt_id

	def finish_attempt(self, attempt_id: int, batch_id: str, position: int, outcome: str, error_code: str = "") -> None:
		self._conn.execute(
			"UPDATE write_attempts SET outcome = ?, error_code = ? WHERE attempt_id = ?",
			(outcome, error_code, attempt_id),
		)
		self._conn.execute(
			"UPDATE batch_items SET status = ?, outcome = ?, error_code = ?, security_id_cipher = '' WHERE batch_id = ? AND position = ?",
			(outcome, outcome, error_code, batch_id, position),
		)
		self._conn.commit()

	def finish_batch(self, batch_id: str, status: str, reason: str = "") -> None:
		completed = now_ts()
		self._conn.execute(
			"UPDATE batches SET status = ?, stopped_reason = ?, completed_at = ? WHERE batch_id = ?",
			(status, reason, completed, batch_id),
		)
		self._conn.execute("UPDATE batch_items SET security_id_cipher = '' WHERE batch_id = ?", (batch_id,))
		self._conn.commit()

	def history(self, limit: int = 20) -> list[dict[str, Any]]:
		rows = self._conn.execute(
			"SELECT batch_id, created_at, expires_at, status, stopped_reason, completed_at FROM batches ORDER BY created_at DESC LIMIT ?",
			(max(1, limit),),
		).fetchall()
		result = []
		for row in rows:
			item = dict(row)
			item["attempts"] = self.batch_attempts(item["batch_id"])
			result.append(item)
		return result

	def batch_items_public(self, batch_id: str) -> list[dict[str, Any]]:
		return [
			{
				"position": row["position"],
				"title": row["title"],
				"company": row["company"],
				"city": row["city"],
				"district": row["district"],
				"salary": row["salary"],
				"track_label": row["track_label"],
				"query": row["query"],
				"message": row["message"],
				"status": row["status"],
			}
			for row in self._conn.execute(
				"SELECT * FROM batch_items WHERE batch_id = ? ORDER BY position", (batch_id,)
			).fetchall()
		]
