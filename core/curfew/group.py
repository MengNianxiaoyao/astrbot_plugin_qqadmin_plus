"""单群宵禁任务：起止两个 APScheduler Cron job，状态翻转持锁防并发。"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime

from aiocqhttp import CQHttp
from apscheduler.job import Job
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from astrbot.api import logger

from ...utils import parse_clock


def _hhmm(value: str) -> tuple[int, int]:
    """解析 "HH:MM"；调用方保证已校验，非法值直接抛 ValueError。"""
    parsed = parse_clock(value)
    if parsed is None:
        raise ValueError(f"非法时间: {value}")
    return parsed


def in_curfew_window(now: datetime, start_time: str, end_time: str) -> bool:
    """当前时刻是否落在宵禁时间段内（纯函数，支持跨天，如 22:00~06:00）。"""
    hour_s, minute_s = _hhmm(start_time)
    hour_e, minute_e = _hhmm(end_time)
    start_dt = now.replace(hour=hour_s, minute=minute_s, second=0, microsecond=0)
    end_dt = now.replace(hour=hour_e, minute=minute_e, second=0, microsecond=0)
    if start_dt > end_dt:  # 跨天
        return now >= start_dt or now < end_dt
    return start_dt <= now < end_dt


class GroupCurfew:
    """单群宵禁任务，维护两个 job（开始/结束）"""

    def __init__(
        self,
        bot: CQHttp,
        group_id: str,
        start_time: str,
        end_time: str,
        scheduler: AsyncIOScheduler,
        active: bool = False,
        on_status_change: Callable[["GroupCurfew"], Awaitable[None]] | None = None,
    ):
        self.bot = bot
        self.group_id = group_id
        self._start_time_str = start_time
        self._end_time_str = end_time
        self.scheduler = scheduler
        self.start_job: Job | None = None
        self.end_job: Job | None = None
        # 持久化的“当前是否已全员禁言”状态：重启后据此补解禁/补启用
        self.whole_ban_status = active
        self._on_status_change = on_status_change
        self._lock = asyncio.Lock()

    async def _notify_status_change(self):
        if self._on_status_change is None:
            return
        try:
            await self._on_status_change(self)
        except Exception as e:
            logger.warning(f"群 {self.group_id} 宵禁状态保存失败: {e}")

    async def _set_whole_ban(self, enable: bool, notice: str | None):
        """翻转全员禁言：成功后发通知，失败回滚状态并保留任务以便下次重试。"""
        async with self._lock:
            if self.whole_ban_status == enable:
                return
            self.whole_ban_status = enable
        try:
            await self.bot.set_group_whole_ban(group_id=int(self.group_id), enable=enable)
        except Exception as e:
            action = "开启" if enable else "解除"
            logger.error(f"群 {self.group_id} 宵禁{action}失败: {e}", exc_info=True)
            async with self._lock:
                self.whole_ban_status = not enable
            return

        # 通知失败不应回滚已经生效的禁言状态
        if notice:
            try:
                await self.bot.send_group_msg(group_id=int(self.group_id), message=notice)
            except Exception as e:
                logger.warning(f"群 {self.group_id} 宵禁通知发送失败: {e}")
        logger.info(f"群 {self.group_id} 已{'开启' if enable else '解除'}全体禁言")
        await self._notify_status_change()

    async def _enable_curfew(self):
        """开启宵禁"""
        await self._set_whole_ban(True, f"【{self._start_time_str}】本群宵禁开始！")

    async def _disable_curfew(self, notify: bool = True):
        """关闭宵禁；手动关闭时不发定时结束通知。"""
        await self._set_whole_ban(False, f"【{self._end_time_str}】本群宵禁结束！" if notify else None)

    async def start_curfew_task(self):
        """注册 APScheduler 定时任务"""
        hour_s, minute_s = _hhmm(self._start_time_str)
        hour_e, minute_e = _hhmm(self._end_time_str)

        self.start_job = self.scheduler.add_job(
            self._enable_curfew,
            trigger=CronTrigger(hour=hour_s, minute=minute_s),
            name=f"curfew_start_{self.group_id}",
            misfire_grace_time=60,  # 如果错过 60 秒内仍执行
            replace_existing=True,
        )
        self.end_job = self.scheduler.add_job(
            self._disable_curfew,
            trigger=CronTrigger(hour=hour_e, minute=minute_e),
            name=f"curfew_end_{self.group_id}",
            misfire_grace_time=60,
            replace_existing=True,
        )
        # 按“当前是否在窗口内”对齐状态：跨天窗口用纯函数判断；
        # 若上次进程留下了禁言（错过结束任务），启动后补一次解禁，避免群一直被全禁。
        now = datetime.now(self.scheduler.timezone)
        if in_curfew_window(now, self._start_time_str, self._end_time_str):
            logger.debug(f"当前时间群 {self.group_id} 在宵禁时间段，立即启用禁言")
            await self._enable_curfew()
        elif self.whole_ban_status:
            logger.info(f"群 {self.group_id} 已不在宵禁时间段，补解禁上次遗留的全员禁言")
            await self._disable_curfew()

    def stop_curfew_task(self):
        """移除 APScheduler 任务（同步即可）"""
        for job in (self.start_job, self.end_job):
            if job:
                try:
                    job.remove()
                except Exception as e:
                    logger.debug(f"移除宵禁任务失败(可能已移除): {e}")
        self.start_job = None
        self.end_job = None
        logger.info(f"群 {self.group_id} 宵禁任务已移除")
