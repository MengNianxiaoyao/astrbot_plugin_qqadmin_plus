"""单 Bot 宵禁调度：统一管理多群任务与持久化。"""

import asyncio

from aiocqhttp import CQHttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from astrbot.api import logger

from .group import GroupCurfew
from .store import CurfewStore


class BotCurfewManager:
    """单 Bot 宵禁调度，统一管理多群"""

    def __init__(self, bot: CQHttp, bot_id: str, store: CurfewStore, scheduler: AsyncIOScheduler):
        self.bot = bot
        self.bot_id = bot_id
        self.store = store
        self.scheduler = scheduler
        self.store.data.setdefault(bot_id, {})
        self.bot_data = self.store.data[bot_id]
        self.tasks: dict[str, GroupCurfew] = {}
        self._save_lock = asyncio.Lock()

    async def restore_from_store(self):
        """恢复群聊禁言任务"""
        for group_id, times in self.bot_data.items():
            try:
                cw = GroupCurfew(
                    self.bot,
                    group_id,
                    times["start_time"],
                    times["end_time"],
                    self.scheduler,
                )
                await cw.start_curfew_task()
                self.tasks[group_id] = cw
            except Exception as e:
                logger.error(f"恢复群 {group_id} 宵禁失败: {e}")

    async def _save(self):
        async with self._save_lock:
            self.bot_data.clear()
            self.bot_data.update({gid: {"start_time": cw._start_time_str, "end_time": cw._end_time_str} for gid, cw in self.tasks.items()})
            await self.store.save_async()

    async def enable_curfew(self, group_id: str, start_time: str, end_time: str):
        """创建群聊的宵禁任务"""
        if group_id in self.tasks:
            self.tasks[group_id].stop_curfew_task()
        cw = GroupCurfew(self.bot, group_id, start_time, end_time, self.scheduler)

        await cw.start_curfew_task()
        self.tasks[group_id] = cw
        await self._save()

    async def disable_curfew(self, group_id: str) -> bool:
        """关闭群聊的宵禁任务"""
        cw = self.tasks.pop(group_id, None)
        if cw:
            cw.stop_curfew_task()
            self.bot_data.pop(group_id, None)
            await self._save()
            return True
        return False
