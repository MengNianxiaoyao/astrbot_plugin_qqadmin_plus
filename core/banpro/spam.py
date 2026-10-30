"""刷屏模块：滑动窗口状态与刷屏禁言执法。"""

import time
from collections import deque

from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from ...config import PluginConfig
from ...data import QQAdminDB
from ...utils import get_nickname, is_group_admin

# 状态清理检查的最小间隔（秒）：避免每条群消息都全量扫一遍
_SWEEP_INTERVAL = 60


class SpamDetector:
    """刷屏模块：滑动窗口状态与刷屏禁言执法。"""

    def __init__(self, config: PluginConfig, db: QQAdminDB):
        self.cfg = config
        self.db = db
        # 不用 maxlen 固定，动态读取 cfg.spamming_count，便于热更新与手动裁剪
        self.msg_timestamps: dict[str, dict[str, deque[float]]] = {}
        self.last_banned_time: dict[str, dict[str, float]] = {}
        self._last_sweep: dict[str, float] = {}

    async def spamming_ban(self, event: AiocqhttpMessageEvent):
        """刷屏禁言"""
        group_id = event.get_group_id()
        sender_id = str(event.get_sender_id())
        snapshot = self.db.get_group_snapshot(group_id)
        ban_time = snapshot.get("spamming_ban_time", 0)
        if sender_id == str(event.get_self_id()) or ban_time <= 0 or len(event.get_messages()) == 0:
            return
        if sender_id in self.cfg.admins_id or is_group_admin(event):
            return

        now = time.time()
        group_banned = self.last_banned_time.setdefault(group_id, {})
        if now - group_banned.get(sender_id, 0) < ban_time:
            return

        group_times = self.msg_timestamps.setdefault(group_id, {})
        timestamps = group_times.setdefault(sender_id, deque())
        timestamps.append(now)
        count = int(self.cfg.spamming_count)
        interval_thr = float(self.cfg.spamming_interval)
        # 手动按 count 裁剪，支持配置热更新
        while len(timestamps) > count:
            timestamps.popleft()
        if len(timestamps) >= count:
            recent = list(timestamps)[-count:]
            intervals = [recent[i + 1] - recent[i] for i in range(count - 1)]
            if all(interval < interval_thr for interval in intervals):
                # 提前写入禁止标记，防止并发重复禁
                group_banned[sender_id] = now

                try:
                    await event.bot.set_group_ban(
                        group_id=int(group_id),
                        user_id=int(sender_id),
                        duration=ban_time,
                    )
                    nickname = await get_nickname(event, sender_id)
                    await event.send(event.plain_result(f"检测到{nickname}刷屏，已禁言"))
                except Exception:
                    logger.error(f"bot在群{group_id}权限不足，禁言失败")
                timestamps.clear()

        self._sweep(group_id, ban_time, now)

    def _sweep(self, group_id: str, ban_time: int, now: float):
        """定期清理长期不活跃用户，避免按用户/群无限累积状态。"""
        if now - self._last_sweep.get(group_id, 0) < _SWEEP_INTERVAL:
            return
        self._last_sweep[group_id] = now
        idle_limit = max(ban_time, 3600)
        cutoff = now - idle_limit

        group_times = self.msg_timestamps.get(group_id)
        if group_times:
            for uid, stamps in list(group_times.items()):
                if not stamps or stamps[-1] < cutoff:
                    group_times.pop(uid, None)
            if not group_times:
                self.msg_timestamps.pop(group_id, None)

        group_banned = self.last_banned_time.get(group_id)
        if group_banned:
            for uid, banned_at in list(group_banned.items()):
                if banned_at < cutoff:
                    group_banned.pop(uid, None)
            if not group_banned:
                self.last_banned_time.pop(group_id, None)

        if group_id not in self.msg_timestamps and group_id not in self.last_banned_time:
            self._last_sweep.pop(group_id, None)
