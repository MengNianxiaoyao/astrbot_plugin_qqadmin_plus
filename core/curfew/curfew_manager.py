"""单 Bot 宵禁调度：统一管理多群任务与持久化。"""

import asyncio

from aiocqhttp import CQHttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from astrbot.api import logger

from .curfew_store import CurfewStore
from .group import GroupCurfew


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
        """恢复群聊禁言任务（两阶段：先全部登记再启动，避免启动中的保存丢任务）。"""
        restored: list[GroupCurfew] = []
        for group_id, times in list(self.bot_data.items()):
            try:
                cw = GroupCurfew(
                    self.bot,
                    group_id,
                    times["start_time"],
                    times["end_time"],
                    self.scheduler,
                    active=bool(times.get("active", False)),
                    on_status_change=self._on_status_change,
                )
                self.tasks[group_id] = cw
                restored.append(cw)
            except Exception as e:
                logger.error(f"恢复群 {group_id} 宵禁失败: {e}")

        for cw in restored:
            try:
                await cw.start_curfew_task()
            except Exception as e:
                logger.error(f"恢复群 {cw.group_id} 宵禁失败: {e}")

    async def _on_status_change(self, _cw: GroupCurfew):
        """任务状态翻转后落盘，重启时据此补启用/补解禁。"""
        await self._save()

    async def _save(self):
        async with self._save_lock:
            self.bot_data.clear()
            self.bot_data.update(
                {
                    gid: {
                        "start_time": cw._start_time_str,
                        "end_time": cw._end_time_str,
                        "active": cw.whole_ban_status,
                    }
                    for gid, cw in self.tasks.items()
                }
            )
            await self.store.save_async()

    async def enable_curfew(self, group_id: str, start_time: str, end_time: str):
        """创建群聊的宵禁任务；先建新任务成功后再移除旧的，避免中途失败丢任务。"""
        old = self.tasks.get(group_id)
        cw = GroupCurfew(
            self.bot,
            group_id,
            start_time,
            end_time,
            self.scheduler,
            # 改时间段时沿用当前禁言状态，由 start_curfew_task 按新窗口对齐
            active=old.whole_ban_status if old is not None else False,
            on_status_change=self._on_status_change,
        )
        # 先登记再启动：启动过程中若翻转状态会触发保存，此时必须已在 tasks 中
        self.tasks[group_id] = cw
        try:
            await cw.start_curfew_task()
        except Exception:
            if old is not None:
                self.tasks[group_id] = old
            else:
                self.tasks.pop(group_id, None)
            raise
        if old is not None:
            old.stop_curfew_task()
        await self._save()

    async def disable_curfew(self, group_id: str) -> bool:
        """关闭群聊的宵禁任务（窗口内关闭时先解除已生效的全员禁言）"""
        cw = self.tasks.pop(group_id, None)
        if cw:
            cw.stop_curfew_task()
            if cw.whole_ban_status:
                # 用户可能在宵禁窗口内关闭：任务移除后不会再有结束任务补解禁，
                # 必须在这里下发一次解除，否则群会长期停留在全员禁言。
                await cw._disable_curfew(notify=False)
            self.bot_data.pop(group_id, None)
            await self._save()
            return True
        return False
