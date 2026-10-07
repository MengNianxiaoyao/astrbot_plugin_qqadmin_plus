"""刷屏模块：滑动窗口状态、刷屏时长配置与刷屏禁言执法。"""

import time
from collections import defaultdict, deque

from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from ...config import PluginConfig
from ...data import QQAdminDB
from ...utils import get_nickname


class SpamDetector:
    """刷屏模块：滑动窗口状态、刷屏时长配置与刷屏禁言执法。"""

    def __init__(self, config: PluginConfig, db: QQAdminDB):
        self.cfg = config
        self.db = db
        # 不用 maxlen 固定，动态读取 cfg.spamming_count，便于热更新与手动裁剪
        self.msg_timestamps: dict[str, dict[str, deque[float]]] = defaultdict(lambda: defaultdict(deque))
        self.last_banned_time: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    async def handle_spamming_ban_time(self, event: AiocqhttpMessageEvent, time: int | None):
        """设置刷屏禁言时长"""
        gid = event.get_group_id()
        if isinstance(time, int):
            await self.db.set(gid, "spamming_ban_time", time)
            msg = f"本群刷屏禁言时长已设为：{time} 秒" if time > 0 else "本群刷屏禁言已关闭"
            await event.send(event.plain_result(msg))
        else:
            status = await self.db.get(gid, "spamming_ban_time", 0)
            await event.send(event.plain_result(f"本群刷屏禁言时长：{status} 秒"))

    async def spamming_ban(self, event: AiocqhttpMessageEvent):
        """刷屏禁言"""
        group_id = event.get_group_id()
        sender_id = event.get_sender_id()
        snapshot = self.db.get_group_snapshot(group_id)
        ban_time = snapshot.get("spamming_ban_time", 0)
        if sender_id == event.get_self_id() or ban_time <= 0 or len(event.get_messages()) == 0:
            return

        now = time.time()

        last_time = self.last_banned_time[group_id][sender_id]
        if now - last_time < ban_time:
            return

        timestamps = self.msg_timestamps[group_id][sender_id]
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
                self.last_banned_time[group_id][sender_id] = now

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
        # 惰性清理：长时间未发言的用户释放内存（覆盖空 deque 与未达阈值的残留）
        # 禁言抑制期内的 last_banned_time 予以保留，避免重复禁言
        idle_limit = max(ban_time, 3600)
        if not timestamps or (now - timestamps[-1] > idle_limit):
            self.msg_timestamps[group_id].pop(sender_id, None)
            if now - self.last_banned_time[group_id].get(sender_id, 0) > idle_limit:
                self.last_banned_time[group_id].pop(sender_id, None)
            if not self.msg_timestamps[group_id]:
                self.msg_timestamps.pop(group_id, None)
            if not self.last_banned_time[group_id]:
                self.last_banned_time.pop(group_id, None)
