"""抽奖持久化：JSON 原子写 + 节流合并，避免每次参与都全量落盘。"""

import asyncio
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ...utils import load_json, safe_int, save_json_atomic
from .models import SAVE_DEBOUNCE, Lottery

# v1 的 history 按群各存一份完整记录，迁移时用这些字段判断是否为同一场
LEGACY_KEY_FIELDS = ("group_id", "prize", "created_at", "drawn_at", "starter")


class LotteryStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = asyncio.Lock()
        self._pending: asyncio.Task | None = None
        self._snapshot: Callable[[], dict[str, Any]] | None = None
        self._version = 0

    def load(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, list[str]]]:
        """读取 active、场次表与每群索引；旧版按群存储的 history 会就地迁移。"""
        data = load_json(self.path, None)
        if not isinstance(data, dict):
            return [], [], {}
        active = data.get("active")
        active_list = active if isinstance(active, list) else []
        history = data.get("history")
        if isinstance(history, list):
            index = data.get("index")
            return active_list, history, index if isinstance(index, dict) else {}
        if isinstance(history, dict):
            records, index = self._migrate_legacy_history(history)
            return active_list, records, index
        return active_list, [], {}

    @staticmethod
    def _migrate_legacy_history(history: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
        """把 v1 按群重复的完整记录去重成“场次表 + 每群场次 ID 索引”。"""
        records: list[dict[str, Any]] = []
        index: dict[str, list[str]] = {}
        ids: dict[tuple, str] = {}
        for gid, items in history.items():
            if not isinstance(items, list):
                continue
            gid_ids: list[str] = []
            for record in items:
                if not isinstance(record, dict):
                    continue
                key = tuple(str(record.get(field) or "") for field in LEGACY_KEY_FIELDS)
                record_id = ids.get(key)
                if record_id is None:
                    record_id = str(record.get("id") or uuid.uuid4().hex[:16])
                    ids[key] = record_id
                    records.append({**record, "id": record_id})
                gid_ids.append(record_id)
            index[str(gid)] = gid_ids
        return records, index

    async def save(self, snapshot: Callable[[], dict[str, Any]], immediate: bool = False):
        """immediate=False 时合并 1 秒内的多次写入，只落盘最新状态。

        snapshot 是工厂：延迟写到时才取最新状态，避免每次参与都全量序列化。
        """
        self._version += 1
        self._snapshot = snapshot
        if immediate:
            await self._flush(self._version)
            return
        if self._pending is None or self._pending.done():
            self._pending = asyncio.create_task(self._delayed_save())

    async def flush(self):
        """停用前把最新快照写盘（不中断正在进行的写，避免旧快照后落盘覆盖新数据）。"""
        pending = self._pending
        await self._flush(self._version)
        if pending is not None and not pending.done():
            pending.cancel()

    async def _delayed_save(self):
        await asyncio.sleep(SAVE_DEBOUNCE)
        await self._flush(self._version)

    async def _flush(self, version: int):
        async with self._lock:
            if version < self._version or self._snapshot is None:
                return
            try:
                snapshot = self._snapshot()
                await asyncio.to_thread(self._write, snapshot)
            except Exception as e:
                logger.error(f"保存抽奖数据失败: {e}")

    def _write(self, data: dict[str, Any]):
        save_json_atomic(self.path, data)

    @staticmethod
    def _opt_float(value: Any) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @classmethod
    def record_to_lottery(cls, record: Any) -> Lottery | None:
        if not isinstance(record, dict):
            return None
        groups = [str(gid) for gid in (record.get("groups") or []) if str(gid)]
        mode = str(record.get("mode") or "")
        if not groups or mode not in {"time", "count", "instant"}:
            return None
        group_id = str(record.get("group_id") or "")
        copies = max(1, safe_int(record.get("copies"), None) or 1)
        participants_raw = record.get("participants")
        participants = (
            {str(uid): str(name) for uid, name in participants_raw.items()}
            if isinstance(participants_raw, dict)
            else {}
        )
        user_groups_raw = record.get("user_groups")
        user_groups = (
            {str(uid): str(gid) for uid, gid in user_groups_raw.items() if str(gid)}
            if isinstance(user_groups_raw, dict)
            else {}
        )
        winners = [
            (str(item[0]), str(item[1]))
            for item in (record.get("winners") or [])
            if isinstance(item, (list, tuple)) and len(item) >= 2
        ]
        failures = [
            (str(item[0]), str(item[1]))
            for item in (record.get("delivery_failures") or [])
            if isinstance(item, (list, tuple)) and len(item) >= 2
        ]
        titles_raw = record.get("titles")
        if isinstance(titles_raw, list):
            titles = [str(item).strip() for item in titles_raw if str(item).strip()]
        else:
            # 兼容旧数据：早期版本只存单个 title
            old_title = str(record.get("title") or "").strip()
            titles = [old_title] if old_title else []
        return Lottery(
            id=str(record.get("id") or uuid.uuid4().hex[:16]),
            group_id=group_id if group_id in groups else groups[0],
            groups=groups,
            prize=str(record.get("prize") or "奖品"),
            copies=copies,
            mode=mode,
            starter=str(record.get("starter") or ""),
            label=str(record.get("label") or ""),
            delivery="private" if record.get("delivery") == "private" else "manual",
            stock=[str(item) for item in (record.get("stock") or []) if str(item)],
            delivery_failures=failures,
            participants=participants,
            user_groups=user_groups,
            participant_count=safe_int(record.get("participant_count"), None) or len(participants),
            winners=winners,
            min_level=safe_int(record.get("min_level"), None) or 0,
            titles=titles,
            expire_at=cls._opt_float(record.get("expire_at")),
            target_count=safe_int(record.get("target_count"), None),
            max_participants=safe_int(record.get("max_participants"), None),
            deadline_label=str(record.get("deadline_label") or ""),
            created_at=cls._opt_float(record.get("created_at")) or 0.0,
            drawn_at=cls._opt_float(record.get("drawn_at")),
            closed=bool(record.get("closed")),
            closing=bool(record.get("closing")),
            bot_id=str(record.get("bot_id") or ""),
        )

    @staticmethod
    def lottery_to_dict(lottery: Lottery) -> dict[str, Any]:
        return {
            "id": lottery.id,
            "group_id": lottery.group_id,
            "groups": lottery.groups,
            "prize": lottery.prize,
            "copies": lottery.copies,
            "mode": lottery.mode,
            "starter": lottery.starter,
            "label": lottery.label,
            "delivery": lottery.delivery,
            "stock": lottery.stock,
            "delivery_failures": [list(item) for item in lottery.delivery_failures],
            "participants": lottery.participants,
            "user_groups": lottery.user_groups,
            "participant_count": lottery.participant_count or len(lottery.participants),
            "winners": [list(item) for item in lottery.winners],
            "min_level": lottery.min_level,
            "titles": lottery.titles,
            "expire_at": lottery.expire_at,
            "target_count": lottery.target_count,
            "max_participants": lottery.max_participants,
            "deadline_label": lottery.deadline_label,
            "created_at": lottery.created_at,
            "drawn_at": lottery.drawn_at,
            "closed": lottery.closed,
            "closing": lottery.closing,
            "bot_id": lottery.bot_id,
        }
