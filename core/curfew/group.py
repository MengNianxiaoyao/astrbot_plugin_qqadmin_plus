"""单群宵禁任务：起止两个 APScheduler Cron job，状态翻转持锁防并发。"""

import asyncio
from datetime import datetime, timedelta

from aiocqhttp import CQHttp
from apscheduler.job import Job
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from astrbot.api import logger


class GroupCurfew:
    """单群宵禁任务，维护两个 job（开始/结束）"""

    def __init__(
        self,
        bot: CQHttp,
        group_id: str,
        start_time: str,
        end_time: str,
        scheduler: AsyncIOScheduler,
    ):
        self.bot = bot
        self.group_id = group_id
        self._start_time_str = start_time
        self._end_time_str = end_time
        self.scheduler = scheduler
        self.start_job: Job | None = None
        self.end_job: Job | None = None
        self.whole_ban_status = False
        self._lock = asyncio.Lock()

    async def _enable_curfew(self):
        """开启宵禁"""
        async with self._lock:
            if self.whole_ban_status:
                return
            self.whole_ban_status = True
        try:
            await self.bot.send_group_msg(
                group_id=int(self.group_id),
                message=f"【{self._start_time_str}】本群宵禁开始！",
            )
            await self.bot.set_group_whole_ban(group_id=int(self.group_id), enable=True)
            logger.info(f"群 {self.group_id} 已开启全体禁言")
        except Exception as e:
            logger.error(f"群 {self.group_id} 宵禁开启失败: {e}", exc_info=True)
            async with self._lock:
                self.whole_ban_status = False
            # 临时异常不自动移除，保留任务以便下次重试

    async def _disable_curfew(self):
        """关闭宵禁"""
        async with self._lock:
            if not self.whole_ban_status:
                return
            self.whole_ban_status = False
        try:
            await self.bot.send_group_msg(
                group_id=int(self.group_id),
                message=f"【{self._end_time_str}】本群宵禁结束！",
            )
            await self.bot.set_group_whole_ban(group_id=int(self.group_id), enable=False)
            logger.info(f"群 {self.group_id} 已解除全体禁言")
        except Exception as e:
            logger.error(f"群 {self.group_id} 宵禁解除失败: {e}", exc_info=True)
            async with self._lock:
                self.whole_ban_status = True

    async def start_curfew_task(self):
        """注册 APScheduler 定时任务"""
        hour_s, minute_s = map(int, self._start_time_str.split(":"))
        hour_e, minute_e = map(int, self._end_time_str.split(":"))

        self.start_job = self.scheduler.add_job(
            self._enable_curfew,
            trigger=CronTrigger(hour=hour_s, minute=minute_s),
            name=f"curfew_start_{self.group_id}",
            misfire_grace_time=60,  # 如果错过 60 秒内仍执行
        )
        self.end_job = self.scheduler.add_job(
            self._disable_curfew,
            trigger=CronTrigger(hour=hour_e, minute=minute_e),
            name=f"curfew_end_{self.group_id}",
            misfire_grace_time=60,
        )
        # 立即检查是否跨天在宵禁时间段
        now = datetime.now(self.scheduler.timezone)
        start_dt = now.replace(hour=hour_s, minute=minute_s, second=0, microsecond=0)
        end_dt = now.replace(hour=hour_e, minute=minute_e, second=0, microsecond=0)
        if start_dt >= end_dt:  # 跨天
            end_dt += timedelta(days=1)
        if start_dt <= now < end_dt:
            logger.debug(f"当前时间群 {self.group_id} 在宵禁时间段，立即启用禁言")
            await self._enable_curfew()

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
