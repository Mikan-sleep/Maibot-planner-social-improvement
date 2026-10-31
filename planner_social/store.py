"""插件本地存储层。

使用插件自己的 SQLite 文件（位于 ``self.ctx.paths.data_dir``），不碰 MaiBot 的
业务数据库，便于升级、备份和卸载。表结构通过 ``SCHEMA_VERSION`` 管理。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .models import MessageRecord

SCHEMA_VERSION = 5


class SocialStore:
    """消息、决策和群策略的持久化。"""

    def __init__(self, path: Path, *, store_text: bool = True, retention_days: int = 7) -> None:
        self.path = Path(path)
        self.store_text = bool(store_text)
        self.retention_days = max(1, int(retention_days))
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 生命周期
    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    def _initialize_sync(self) -> None:
        with self._lock:
            conn = self._connect()
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    group_id TEXT NOT NULL DEFAULT '',
                    user_id TEXT NOT NULL DEFAULT '',
                    user_name TEXT NOT NULL DEFAULT '',
                    text TEXT NOT NULL DEFAULT '',
                    timestamp REAL NOT NULL,
                    is_bot INTEGER NOT NULL DEFAULT 0,
                    reply_to TEXT NOT NULL DEFAULT '',
                    is_at INTEGER NOT NULL DEFAULT 0,
                    is_mentioned INTEGER NOT NULL DEFAULT 0
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_unique
                    ON messages(session_id, message_id);
                CREATE INDEX IF NOT EXISTS idx_messages_session_time
                    ON messages(session_id, timestamp DESC);
                CREATE TABLE IF NOT EXISTS session_groups (
                    session_id TEXT PRIMARY KEY,
                    group_id TEXT NOT NULL DEFAULT '',
                    group_name TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    decision_id TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL,
                    group_id TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    action TEXT NOT NULL,
                    target_message_id TEXT NOT NULL DEFAULT '',
                    utility REAL NOT NULL,
                    threshold REAL NOT NULL,
                    features_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    reward REAL,
                    feedback_at REAL,
                    neutral_count INTEGER NOT NULL DEFAULT 0,
                    reply_text TEXT NOT NULL DEFAULT '',
                    forced INTEGER NOT NULL DEFAULT 0,
                    label_source TEXT NOT NULL DEFAULT '',
                    judge_reason TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_decisions_session_status
                    ON decisions(session_id, status, created_at DESC);                CREATE TABLE IF NOT EXISTS policies (
                    group_id TEXT PRIMARY KEY,
                    weights_json TEXT NOT NULL,
                    positive INTEGER NOT NULL DEFAULT 0,
                    negative INTEGER NOT NULL DEFAULT 0,
                    observations INTEGER NOT NULL DEFAULT 0,
                    prior_source TEXT NOT NULL DEFAULT 'default',
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reflections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    sample_count INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reflections_group_time
                    ON reflections(group_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS frequency_state (
                    group_id TEXT PRIMARY KEY,
                    auto_multiplier REAL NOT NULL DEFAULT 1.0,
                    tuned_at REAL NOT NULL DEFAULT 0,
                    last_reason TEXT NOT NULL DEFAULT ''
                );
                """
            )
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            # 兼容 0.1/0.2 旧库：新增列用 ALTER，不重建用户数据。
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(decisions)").fetchall()}
            for column, ddl in (
                ("reply_text", "TEXT NOT NULL DEFAULT ''"),
                ("forced", "INTEGER NOT NULL DEFAULT 0"),
                ("label_source", "TEXT NOT NULL DEFAULT ''"),
                ("judge_reason", "TEXT NOT NULL DEFAULT ''"),
                ("addressed", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in existing:
                    conn.execute(f"ALTER TABLE decisions ADD COLUMN {column} {ddl}")
            existing_policy = {row["name"] for row in conn.execute("PRAGMA table_info(policies)").fetchall()}
            for column, ddl in (
                ("observations", "INTEGER NOT NULL DEFAULT 0"),
                ("prior_source", "TEXT NOT NULL DEFAULT 'default'"),
            ):
                if column not in existing_policy:
                    conn.execute(f"ALTER TABLE policies ADD COLUMN {column} {ddl}")
            conn.commit()

    async def initialize(self) -> None:
        """创建目录和表结构，并按保留期清理旧数据。"""

        await asyncio.to_thread(self._initialize_sync)
        await self.cleanup()

    def close(self) -> None:
        """关闭连接；插件卸载时调用。"""

        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ------------------------------------------------------------------ 消息
    def _prepare_text(self, text: str) -> str:
        if self.store_text:
            return text[:2000]
        # 不存原文时保留稳定摘要，仍能用于计数/去重，不保留可读内容。
        digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
        return f"<redacted:{digest}:len={len(text)}>"

    def _record_message_sync(self, record: MessageRecord) -> None:
        with self._lock:
            conn = self._connect()
            conn.execute(
                """
                INSERT OR REPLACE INTO messages(
                    message_id, session_id, group_id, user_id, user_name, text,
                    timestamp, is_bot, reply_to, is_at, is_mentioned
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.message_id,
                    record.session_id,
                    record.group_id,
                    record.user_id,
                    record.user_name,
                    self._prepare_text(record.text),
                    record.timestamp,
                    1 if record.is_bot else 0,
                    record.reply_to,
                    1 if record.is_at else 0,
                    1 if record.is_mentioned else 0,
                ),
            )
            conn.commit()

    async def record_message(self, record: MessageRecord) -> None:
        """写入或更新一条消息。"""

        await asyncio.to_thread(self._record_message_sync, record)

    def _get_recent_sync(self, session_id: str, limit: int) -> list[MessageRecord]:
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                """
                SELECT * FROM messages WHERE session_id = ?
                ORDER BY timestamp DESC, id DESC LIMIT ?
                """,
                (session_id, max(1, int(limit))),
            ).fetchall()
        return [self._row_to_message(row) for row in reversed(rows)]

    async def get_recent(self, session_id: str, limit: int = 20) -> list[MessageRecord]:
        """按时间升序返回最近消息。"""

        return await asyncio.to_thread(self._get_recent_sync, session_id, limit)

    def _get_messages_since_sync(self, session_id: str, since: float, limit: int) -> list[MessageRecord]:
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                """
                SELECT * FROM messages
                WHERE session_id = ? AND timestamp >= ?
                ORDER BY timestamp ASC, id ASC LIMIT ?
                """,
                (session_id, float(since), max(1, int(limit))),
            ).fetchall()
        return [self._row_to_message(row) for row in rows]

    async def get_messages_since(self, session_id: str, since: float, limit: int = 8) -> list[MessageRecord]:
        """返回某时间点之后的消息，供 LLM 反馈裁判使用。"""

        return await asyncio.to_thread(self._get_messages_since_sync, session_id, since, limit)

    def _get_message_sync(self, message_id: str) -> MessageRecord | None:
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM messages WHERE message_id = ? ORDER BY id DESC LIMIT 1",
                (message_id,),
            ).fetchone()
        return self._row_to_message(row) if row else None

    async def get_message(self, message_id: str) -> MessageRecord | None:
        """按消息 ID 查询（用于解析 reply_to 目标）。"""

        return await asyncio.to_thread(self._get_message_sync, message_id)

    def _get_last_bot_sync(self, session_id: str) -> MessageRecord | None:
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM messages WHERE session_id = ? AND is_bot = 1 ORDER BY timestamp DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return self._row_to_message(row) if row else None

    async def get_last_bot(self, session_id: str) -> MessageRecord | None:
        """返回该会话最近一条机器人消息（可能已不在最近窗口内）。"""

        return await asyncio.to_thread(self._get_last_bot_sync, session_id)

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> MessageRecord:
        return MessageRecord(
            message_id=str(row["message_id"]),
            session_id=str(row["session_id"]),
            group_id=str(row["group_id"] or ""),
            user_id=str(row["user_id"] or ""),
            user_name=str(row["user_name"] or ""),
            text=str(row["text"] or ""),
            timestamp=float(row["timestamp"]),
            is_bot=bool(row["is_bot"]),
            reply_to=str(row["reply_to"] or ""),
            is_at=bool(row["is_at"]),
            is_mentioned=bool(row["is_mentioned"]),
        )

    # ------------------------------------------------------------------ 会话/群映射
    def _map_group_sync(self, session_id: str, group_id: str, group_name: str) -> None:
        with self._lock:
            conn = self._connect()
            conn.execute(
                """
                INSERT INTO session_groups(session_id, group_id, group_name, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    group_id = excluded.group_id,
                    group_name = excluded.group_name,
                    updated_at = excluded.updated_at
                """,
                (session_id, group_id, group_name, time.time()),
            )
            conn.commit()

    async def map_session_group(self, session_id: str, group_id: str, group_name: str) -> None:
        """记录 session_id 对应的真实群号/群名。"""

        if session_id:
            await asyncio.to_thread(self._map_group_sync, session_id, group_id, group_name)

    def _get_group_sync(self, session_id: str) -> tuple[str, str]:
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT group_id, group_name FROM session_groups WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return "", ""
        return str(row["group_id"] or ""), str(row["group_name"] or "")

    async def get_session_group(self, session_id: str) -> tuple[str, str]:
        """返回 (group_id, group_name)；未知时返回空字符串。"""

        return await asyncio.to_thread(self._get_group_sync, session_id)

    # ------------------------------------------------------------------ 决策
    def _record_decision_sync(
        self,
        *,
        decision_id: str,
        session_id: str,
        group_id: str,
        action: str,
        target_message_id: str,
        utility: float,
        threshold: float,
        features: list[float],
        evidence: list[str],
        status: str,
        forced: bool = False,
        addressed: bool = False,
    ) -> None:
        with self._lock:
            conn = self._connect()
            conn.execute(
                """
                INSERT OR REPLACE INTO decisions(
                    decision_id, session_id, group_id, created_at, action,
                    target_message_id, utility, threshold, features_json,
                    evidence_json, status, forced, addressed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    session_id,
                    group_id,
                    time.time(),
                    action,
                    target_message_id,
                    float(utility),
                    float(threshold),
                    json.dumps(features, ensure_ascii=False),
                    json.dumps(evidence, ensure_ascii=False),
                    status,
                    1 if forced else 0,
                    1 if addressed else 0,
                ),
            )
            conn.commit()

    async def record_decision(
        self,
        *,
        decision_id: str,
        session_id: str,
        group_id: str,
        action: str,
        target_message_id: str,
        utility: float,
        threshold: float,
        features: list[float],
        evidence: list[str],
        status: str = "open",
        forced: bool = False,
        addressed: bool = False,
    ) -> None:
        """记录一次 Planner 决策，供后续反馈学习。"""

        await asyncio.to_thread(
            self._record_decision_sync,
            decision_id=decision_id,
            session_id=session_id,
            group_id=group_id,
            action=action,
            target_message_id=target_message_id,
            utility=utility,
            threshold=threshold,
            features=features,
            evidence=evidence,
            status=status,
            forced=forced,
            addressed=addressed,
        )

    def _get_open_decisions_sync(self, session_id: str, limit: int) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                """
                SELECT * FROM decisions
                WHERE session_id = ? AND status = 'open'
                ORDER BY created_at DESC LIMIT ?
                """,
                (session_id, max(1, int(limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    async def get_open_decisions(self, session_id: str, limit: int = 3) -> list[dict[str, Any]]:
        """返回该会话尚未收到反馈的决策。"""

        return await asyncio.to_thread(self._get_open_decisions_sync, session_id, limit)

    def _get_decision_sync(self, decision_id: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM decisions WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
        return dict(row) if row else None

    async def get_decision(self, decision_id: str) -> dict[str, Any] | None:
        """按 decision_id 查询决策。"""

        return await asyncio.to_thread(self._get_decision_sync, decision_id)

    def _close_decision_sync(
        self,
        decision_id: str,
        reward: float,
        label_source: str = "",
        judge_reason: str = "",
    ) -> None:
        with self._lock:
            conn = self._connect()
            conn.execute(
                """
                UPDATE decisions
                SET status = 'closed', reward = ?, feedback_at = ?,
                    label_source = ?, judge_reason = ?
                WHERE decision_id = ?
                """,
                (float(reward), time.time(), label_source, judge_reason, decision_id),
            )
            conn.commit()

    async def close_decision(
        self,
        decision_id: str,
        reward: float,
        *,
        label_source: str = "",
        judge_reason: str = "",
    ) -> None:
        """关闭决策并写入奖励；label_source 区分 keyword/llm/neutral。"""

        await asyncio.to_thread(
            self._close_decision_sync,
            decision_id,
            reward,
            label_source,
            judge_reason,
        )

    def _claim_decision_sync(self, decision_id: str) -> bool:
        with self._lock:
            conn = self._connect()
            cursor = conn.execute(
                "UPDATE decisions SET status = 'judging' WHERE decision_id = ? AND status = 'open'",
                (decision_id,),
            )
            conn.commit()
        return cursor.rowcount > 0

    async def claim_decision(self, decision_id: str) -> bool:
        """原子占用一条决策，避免多个后台裁判重复处理。"""

        return await asyncio.to_thread(self._claim_decision_sync, decision_id)

    def _attach_reply_text_sync(self, session_id: str, text: str) -> None:
        with self._lock:
            conn = self._connect()
            conn.execute(
                """
                UPDATE decisions SET reply_text = ?
                WHERE id = (
                    SELECT id FROM decisions
                    WHERE session_id = ? AND status IN ('open', 'judging') AND reply_text = ''
                    ORDER BY created_at DESC LIMIT 1
                )
                """,
                (text[:4000], session_id),
            )
            conn.commit()

    async def attach_reply_text(self, session_id: str, text: str) -> None:
        """把机器人实际发送的文本挂到最近的待反馈决策上。"""

        await asyncio.to_thread(self._attach_reply_text_sync, session_id, text)

    def _increment_neutral_sync(self, decision_id: str, neutral_limit: int) -> bool:
        with self._lock:
            conn = self._connect()
            conn.execute(
                "UPDATE decisions SET neutral_count = neutral_count + 1 WHERE decision_id = ?",
                (decision_id,),
            )
            row = conn.execute(
                "SELECT neutral_count FROM decisions WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
            conn.commit()
        return bool(row and int(row["neutral_count"]) >= neutral_limit)

    async def increment_neutral(self, decision_id: str, neutral_limit: int) -> bool:
        """中性消息计数；达到阈值返回 True，由调用方标记弱负反馈。"""

        return await asyncio.to_thread(self._increment_neutral_sync, decision_id, neutral_limit)

    # ------------------------------------------------------------------ 策略
    def _get_policy_sync(self, group_id: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connect()
            row = conn.execute("SELECT * FROM policies WHERE group_id = ?", (group_id,)).fetchone()
        if row is None:
            return None
        return {
            "weights": json.loads(row["weights_json"]),
            "positive": int(row["positive"]),
            "negative": int(row["negative"]),
            "observations": int(row["observations"] or 0),
            "prior_source": str(row["prior_source"] or "default"),
            "updated_at": float(row["updated_at"]),
        }

    async def get_policy(self, group_id: str) -> dict[str, Any] | None:
        """读取群策略；没有时返回 None。"""

        return await asyncio.to_thread(self._get_policy_sync, group_id)

    def _global_prior_sync(self, min_groups: int) -> dict[str, Any] | None:
        """平均有反馈群的权重，作为新群的跨群先验；不共享消息内容。"""

        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT weights_json, positive, negative FROM policies WHERE positive + negative > 0"
            ).fetchall()
        if len(rows) < max(1, int(min_groups)):
            return None
        vectors: list[list[float]] = []
        for row in rows:
            try:
                weights = json.loads(row["weights_json"])
                if isinstance(weights, list) and weights:
                    vectors.append([float(value) for value in weights])
            except (TypeError, ValueError):
                continue
        if not vectors or len({len(vector) for vector in vectors}) != 1:
            return None
        count = len(vectors)
        averaged = [sum(vector[index] for vector in vectors) / count for index in range(len(vectors[0]))]
        return {
            "weights": averaged,
            "positive": sum(int(row["positive"]) for row in rows),
            "negative": sum(int(row["negative"]) for row in rows),
            "observations": sum(int(row["positive"]) + int(row["negative"]) for row in rows),
            "prior_source": "global",
            "updated_at": time.time(),
        }

    async def get_global_prior(self, min_groups: int = 2) -> dict[str, Any] | None:
        """返回跨群平均先验；样本不足时返回 None。"""

        return await asyncio.to_thread(self._global_prior_sync, min_groups)

    def _add_reflection_sync(self, group_id: str, text: str, sample_count: int) -> None:
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT INTO reflections(group_id, text, sample_count, created_at) VALUES (?, ?, ?, ?)",
                (group_id, text[:1200], int(sample_count), time.time()),
            )
            conn.commit()

    async def add_reflection(self, group_id: str, text: str, sample_count: int) -> None:
        """保存一次群规范反思；只保留文本，不保存原始对话。"""

        await asyncio.to_thread(self._add_reflection_sync, group_id, text, sample_count)

    def _get_latest_reflection_sync(self, group_id: str) -> str:
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT text FROM reflections WHERE group_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                (group_id,),
            ).fetchone()
        return str(row["text"] or "") if row else ""

    async def get_latest_reflection(self, group_id: str) -> str:
        """返回该群最新的 LLM 反思文本；没有时为空字符串。"""

        return await asyncio.to_thread(self._get_latest_reflection_sync, group_id)

    def _get_frequency_state_sync(self, group_id: str) -> dict[str, Any]:
        with self._lock:
            conn = self._connect()
            row = conn.execute("SELECT * FROM frequency_state WHERE group_id = ?", (group_id,)).fetchone()
        if row is None:
            return {"auto_multiplier": 1.0, "tuned_at": 0.0, "reason": ""}
        return {
            "auto_multiplier": float(row["auto_multiplier"]),
            "tuned_at": float(row["tuned_at"]),
            "reason": str(row["last_reason"] or ""),
        }

    async def get_frequency_state(self, group_id: str) -> dict[str, Any]:
        """读取该群的自动频率倍率。"""

        return await asyncio.to_thread(self._get_frequency_state_sync, group_id)

    def _save_frequency_state_sync(self, group_id: str, multiplier: float, reason: str) -> None:
        with self._lock:
            conn = self._connect()
            conn.execute(
                """
                INSERT INTO frequency_state(group_id, auto_multiplier, tuned_at, last_reason)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET
                    auto_multiplier = excluded.auto_multiplier,
                    tuned_at = excluded.tuned_at,
                    last_reason = excluded.last_reason
                """,
                (group_id, float(multiplier), time.time(), reason[:300]),
            )
            conn.commit()

    async def save_frequency_state(self, group_id: str, multiplier: float, reason: str) -> None:
        """保存自动频率倍率与最近一次原因。"""

        await asyncio.to_thread(self._save_frequency_state_sync, group_id, multiplier, reason)

    def _save_policy_sync(self, group_id: str, payload: dict[str, Any]) -> None:
        with self._lock:
            conn = self._connect()
            conn.execute(
                """
                INSERT INTO policies(group_id, weights_json, positive, negative, observations, prior_source, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET
                    weights_json = excluded.weights_json,
                    positive = excluded.positive,
                    negative = excluded.negative,
                    observations = excluded.observations,
                    prior_source = excluded.prior_source,
                    updated_at = excluded.updated_at
                """,
                (
                    group_id,
                    json.dumps(payload.get("weights") or [], ensure_ascii=False),
                    int(payload.get("positive") or 0),
                    int(payload.get("negative") or 0),
                    int(payload.get("observations") or (int(payload.get("positive") or 0) + int(payload.get("negative") or 0))),
                    str(payload.get("prior_source") or "default"),
                    float(payload.get("updated_at") or time.time()),
                ),
            )
            conn.commit()

    async def save_policy(self, group_id: str, payload: dict[str, Any]) -> None:
        """保存群策略。"""

        await asyncio.to_thread(self._save_policy_sync, group_id, payload)

    def _delete_policy_sync(self, group_id: str) -> None:
        with self._lock:
            conn = self._connect()
            if group_id:
                conn.execute("DELETE FROM policies WHERE group_id = ?", (group_id,))
            else:
                conn.execute("DELETE FROM policies")
            conn.commit()

    async def reset_policy(self, group_id: str = "") -> None:
        """重置一个群或全部群的策略。"""

        await asyncio.to_thread(self._delete_policy_sync, group_id)

    def _stats_sync(self, group_id: str) -> dict[str, Any]:
        with self._lock:
            conn = self._connect()
            policy = conn.execute("SELECT * FROM policies WHERE group_id = ?", (group_id,)).fetchone()
            decisions = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN action = 'reply' THEN 1 ELSE 0 END) AS replies,
                    SUM(CASE WHEN reward = 1 THEN 1 ELSE 0 END) AS positive,
                    SUM(CASE WHEN reward = -1 THEN 1 ELSE 0 END) AS negative
                FROM decisions WHERE group_id = ?
                """,
                (group_id,),
            ).fetchone()
        return {
            "policy": dict(policy) if policy else None,
            "decisions": dict(decisions) if decisions else {},
        }

    async def stats(self, group_id: str) -> dict[str, Any]:
        """返回一个群的策略和决策计数。"""

        return await asyncio.to_thread(self._stats_sync, group_id)

    def _metrics_sync(self, group_id: str, window_hours: int) -> dict[str, Any]:
        since = time.time() - max(1, int(window_hours)) * 3600
        with self._lock:
            conn = self._connect()
            if group_id:
                session_rows = conn.execute(
                    "SELECT session_id FROM session_groups WHERE group_id = ?", (group_id,)
                ).fetchall()
                sessions = [str(row["session_id"]) for row in session_rows]
            else:
                sessions = []
            if sessions:
                placeholders = ",".join("?" for _ in sessions)
                message_sql = (
                    "SELECT COUNT(*) AS total, "
                    "SUM(CASE WHEN is_bot = 1 THEN 1 ELSE 0 END) AS bots, "
                    "SUM(CASE WHEN is_at = 1 OR is_mentioned = 1 THEN 1 ELSE 0 END) AS addressed "
                    f"FROM messages WHERE timestamp >= ? AND session_id IN ({placeholders})"
                )
                message_row = conn.execute(message_sql, (since, *sessions)).fetchone()
            elif group_id:
                message_row = conn.execute(
                    "SELECT 0 AS total, 0 AS bots, 0 AS addressed"
                ).fetchone()
            else:
                message_row = conn.execute(
                    "SELECT COUNT(*) AS total, "
                    "SUM(CASE WHEN is_bot = 1 THEN 1 ELSE 0 END) AS bots, "
                    "SUM(CASE WHEN is_at = 1 OR is_mentioned = 1 THEN 1 ELSE 0 END) AS addressed "
                    "FROM messages WHERE timestamp >= ?",
                    (since,),
                ).fetchone()
            decision_where = "created_at >= ?" + (" AND group_id = ?" if group_id else "")
            params: tuple[Any, ...] = (since, group_id) if group_id else (since,)
            decision_row = conn.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN action = 'reply' THEN 1 ELSE 0 END) AS replies, "
                "SUM(CASE WHEN forced = 1 THEN 1 ELSE 0 END) AS forced, "
                "SUM(CASE WHEN addressed = 1 AND action = 'reply' THEN 1 ELSE 0 END) AS addressed_replies, "
                "SUM(CASE WHEN reward = -1 THEN 1 ELSE 0 END) AS negative "
                f"FROM decisions WHERE {decision_where}",
                params,
            ).fetchone()
        total = int(message_row["total"] or 0)
        bots = int(message_row["bots"] or 0)
        addressed = int(message_row["addressed"] or 0)
        replies = int(decision_row["replies"] or 0)
        addressed_replies = int(decision_row["addressed_replies"] or 0)
        negative = int(decision_row["negative"] or 0)
        return {
            "window_hours": max(1, int(window_hours)),
            "users": max(total - bots, 0),
            "bot_messages": bots,
            "addressed_messages": addressed,
            "decisions": int(decision_row["total"] or 0),
            "replies": replies,
            "forced": int(decision_row["forced"] or 0),
            "negative": negative,
            "reply_rate": round(bots / max(total - bots, 1), 4),
            "addressed_response_rate": round(addressed_replies / max(addressed, 1), 4),
            "negative_rate": round(negative / max(replies, 1), 4),
        }

    async def metrics(self, group_id: str = "", window_hours: int = 24) -> dict[str, Any]:
        """返回近期回复率/点名响应率等运营指标。"""

        return await asyncio.to_thread(self._metrics_sync, group_id, window_hours)

    # ------------------------------------------------------------------ 设置覆盖
    def _get_setting_sync(self, key: str, default: Any) -> Any:
        with self._lock:
            conn = self._connect()
            row = conn.execute("SELECT value_json FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value_json"])
        except (TypeError, ValueError):
            return default

    async def get_setting(self, key: str, default: Any) -> Any:
        """读取插件运行时设置；没有覆盖时返回默认值。"""

        return await asyncio.to_thread(self._get_setting_sync, key, default)

    def _set_setting_sync(self, key: str, value: Any) -> None:
        with self._lock:
            conn = self._connect()
            conn.execute(
                """
                INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (key, json.dumps(value, ensure_ascii=False), time.time()),
            )
            conn.commit()

    async def set_setting(self, key: str, value: Any) -> None:
        """保存插件运行时设置（与 config.toml 解耦，WebUI 调试不会改文件）。"""

        await asyncio.to_thread(self._set_setting_sync, key, value)

    def _get_sessions_sync(self) -> list[dict[str, str]]:
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT session_id, group_id, group_name FROM session_groups"
            ).fetchall()
        return [dict(row) for row in rows]

    async def list_sessions(self) -> list[dict[str, str]]:
        """返回已见过的会话/群映射。"""

        return await asyncio.to_thread(self._get_sessions_sync)

    # ------------------------------------------------------------------ 决策列表
    def _list_decisions_sync(self, limit: int, group_id: str) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            if group_id:
                rows = conn.execute(
                    "SELECT * FROM decisions WHERE group_id = ? ORDER BY created_at DESC LIMIT ?",
                    (group_id, max(1, int(limit))),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM decisions ORDER BY created_at DESC LIMIT ?",
                    (max(1, int(limit)),),
                ).fetchall()
        return [dict(row) for row in rows]

    async def list_decisions(self, limit: int = 20, group_id: str = "") -> list[dict[str, Any]]:
        """返回最近的决策（供 WebUI 表格）。"""

        return await asyncio.to_thread(self._list_decisions_sync, limit, group_id)

    # ------------------------------------------------------------------ 保留期
    def _cleanup_sync(self) -> None:
        cutoff = time.time() - self.retention_days * 86400
        with self._lock:
            conn = self._connect()
            conn.execute("DELETE FROM messages WHERE timestamp < ?", (cutoff,))
            conn.execute("DELETE FROM decisions WHERE created_at < ?", (cutoff,))
            conn.commit()

    async def cleanup(self) -> None:
        """按 retention_days 清理旧数据。"""

        await asyncio.to_thread(self._cleanup_sync)
