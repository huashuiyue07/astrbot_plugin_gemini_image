"""按用户维度的冷却与每日配额。

生图很贵（一次几十秒、还会消耗账号额度），群聊里不加限制很容易被刷爆，
所以这里做两层限制：
- 冷却：同一个人的两次请求之间必须有间隔；
- 配额：每人每天的调用次数上限，跨天自动重置。

数据落在插件数据目录的 json 文件里，重启不丢。
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date
from pathlib import Path

from astrbot.api import logger


class UsageLimiter:
    def __init__(
        self,
        store_path: str | Path,
        *,
        cooldown_seconds: int = 30,
        daily_quota: int = 0,
        global_daily_quota: int = 0,
    ) -> None:
        self._path = Path(store_path)
        self.cooldown_seconds = max(0, int(cooldown_seconds))
        # 0 表示不限制
        self.daily_quota = max(0, int(daily_quota))
        #: 全员每天的总调用上限，0 = 不限制。
        #: 单人配额挡不住「人多时被轮流刷」，总量上限才是账号安全的最后一道闸。
        self.global_daily_quota = max(0, int(global_daily_quota))
        self._lock = asyncio.Lock()
        self._state: dict = {"date": self._today(), "users": {}}
        self._load()

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _today() -> str:
        return date.today().isoformat()

    def _load(self) -> None:
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, ValueError):
            return
        if isinstance(data, dict) and isinstance(data.get("users"), dict):
            self._state = {
                "date": data.get("date") or self._today(),
                "users": data["users"],
            }
        self._rollover_if_needed()

    def _rollover_if_needed(self) -> None:
        """跨天则清空用量（惰性执行，不需要定时任务）。"""
        today = self._today()
        if self._state.get("date") != today:
            self._state = {"date": today, "users": {}}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._state, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning(f"[gemini-image] 限流数据写入失败：{exc}")

    def _read(self, user_id: str) -> dict:
        """只读查询；查不到就当作「本日未使用」，不产生记录。"""
        users = self._state.get("users") or {}
        record = users.get(user_id)
        return record if isinstance(record, dict) else {}

    def _write_record(self, user_id: str) -> dict:
        users = self._state.setdefault("users", {})
        record = users.get(user_id)
        if not isinstance(record, dict):
            record = {"count": 0, "last": 0.0}
            users[user_id] = record
        return record

    def _total_locked(self) -> int:
        """今日全站调用总数（需在持锁状态下调用）。"""
        users = self._state.get("users") or {}
        return sum(int(r.get("count") or 0) for r in users.values() if isinstance(r, dict))

    # ------------------------------------------------------------------ 对外

    async def check(self, user_id: str) -> str | None:
        """返回拒绝理由（可直接发给用户），None 表示放行。"""
        async with self._lock:
            self._rollover_if_needed()
            record = self._read(user_id)

            remaining = self.cooldown_seconds - (time.time() - float(record.get("last") or 0))
            if remaining > 0:
                return f"⏳ 冷却中，请等 {int(remaining) + 1} 秒后再试"

            if self.daily_quota and int(record.get("count") or 0) >= self.daily_quota:
                return f"🚫 今日生图次数已用完（{self.daily_quota} 次/天），明天再来吧"

            if self.global_daily_quota and self._total_locked() >= self.global_daily_quota:
                return f"🚫 今天的生图总量已达上限（{self.global_daily_quota} 次），明天再来吧"
            return None

    async def consume(self, user_id: str) -> None:
        """记一次成功调用的用量。"""
        async with self._lock:
            self._rollover_if_needed()
            record = self._write_record(user_id)
            record["count"] = int(record.get("count") or 0) + 1
            record["last"] = time.time()
            await asyncio.to_thread(self._save)

    async def reset(self, user_id: str | None = None) -> int:
        """重置用量，返回被清理的用户数（`None` 表示全部）。"""
        async with self._lock:
            self._rollover_if_needed()
            users = self._state.setdefault("users", {})
            if user_id is None:
                cleared = len(users)
                users.clear()
            else:
                cleared = 1 if users.pop(user_id, None) is not None else 0
            await asyncio.to_thread(self._save)
            return cleared

    async def usage(self, user_id: str) -> tuple[int, int]:
        """返回 (今日已用, 每日上限)，上限为 0 表示不限。"""
        async with self._lock:
            self._rollover_if_needed()
            record = self._read(user_id)
            return int(record.get("count") or 0), self.daily_quota

    async def total_usage(self) -> int:
        """今日全站调用总数。"""
        async with self._lock:
            self._rollover_if_needed()
            return self._total_locked()
