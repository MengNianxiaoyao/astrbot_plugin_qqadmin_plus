"""宵禁门面：多 Bot 总管（初始化/命令入口/时间解析/全局启停）。

子模块：store.CurfewStore / group.GroupCurfew / manager.BotCurfewManager。
新人找定时逻辑去 group，找多群管理去 manager，这里只做装配与命令入口。
"""

import asyncio
import zoneinfo

from aiocqhttp import Event
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (
    AiocqhttpAdapter,
)
from astrbot.core.star.context import Context

from ...config import PluginConfig
from .manager import BotCurfewManager
from .store import CurfewStore

__all__ = ["BotCurfewManager", "CurfewHandle", "CurfewStore"]


class CurfewHandle:
    """多 Bot 宵禁处理类"""

    def __init__(self, context: Context, config: PluginConfig):
        self.context = context
        tz = self.context.get_config().get("timezone")
        self.timezone = zoneinfo.ZoneInfo(tz) if tz else zoneinfo.ZoneInfo("Asia/Shanghai")
        self.scheduler = AsyncIOScheduler(timezone=self.timezone)
        self.scheduler.start()
        self.store = CurfewStore(config.curfew_file)
        self.store.load()
        self.curfew_managers: dict[str, BotCurfewManager] = {}

    async def _initialize_aiocqhttp_adapter(self, inst: AiocqhttpAdapter):
        """初始化单个 AiocqhttpAdapter 的宵禁管理器"""
        try:
            client = inst.get_client()
        except Exception as e:
            logger.warning(f"{inst.metadata.id} 获取 aiocqhttp client 失败: {e}")
            return

        if client is None:
            logger.warning(f"{inst.metadata.id} 暂无可用 client（WebSocket 未就绪），宵禁初始化跳过")
            return

        bot_id = None

        # client 直接获取 bot_id
        try:
            login_data = await client.get_login_info() or {}
            user_id = login_data.get("user_id")
            bot_id = str(user_id) if user_id is not None else None
        except Exception:
            pass

        # client 在 ws 连接成功时获取
        if not bot_id:
            loop = asyncio.get_running_loop()
            bot_id_future = loop.create_future()

            @client.on_websocket_connection
            async def on_ws_connect(event_: Event):
                if not bot_id_future.done():
                    bot_id_future.set_result(str(event_.self_id))

            try:
                bot_id = await asyncio.wait_for(bot_id_future, timeout=25)
            except TimeoutError:
                logger.warning(f"{inst.metadata.id} 等待 WebSocket 连接超时")
                return

        # 宵禁初始化
        try:
            self.store.data.setdefault(bot_id, {})
            curfew_mgr = BotCurfewManager(client, bot_id, self.store, self.scheduler)
            self.curfew_managers[bot_id] = curfew_mgr
            await curfew_mgr.restore_from_store()
            logger.debug(f"{inst.metadata.id}({bot_id}) 宵禁初始化完成")
        except Exception as e:
            logger.error(f"{inst.metadata.id} 宵禁初始化失败: {e}")

    async def initialize(self):
        tasks = [self._initialize_aiocqhttp_adapter(inst) for inst in self.context.platform_manager.platform_insts if isinstance(inst, AiocqhttpAdapter)]
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    logger.error(f"宵禁初始化子任务异常: {r}")

    @staticmethod
    def parse_time(time_str: str) -> tuple[str, int, int] | None:
        """
        统一处理时间格式
        输入: "HH:MM" 或带中文冒号 "HH：MM"
        返回: (原始字符串, hour:int, minute:int)
        出错返回 None
        """
        try:
            time_str_clean = time_str.strip().replace("：", ":")
            hour, minute = map(int, time_str_clean.split(":"))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                return None
            return time_str_clean, hour, minute
        except Exception:
            return None

    async def start_curfew(
        self,
        event: AiocqhttpMessageEvent,
        input_start_time: str | None = None,
        input_end_time: str | None = None,
    ):
        if not input_start_time or not input_end_time:
            return "未输入范围 HH:MM HH:MM"

        start_parsed = self.parse_time(input_start_time)
        end_parsed = self.parse_time(input_end_time)

        if not start_parsed or not end_parsed:
            return "时间格式错误，应为 HH:MM"

        start_str, start_h, start_m = start_parsed
        end_str, end_h, end_m = end_parsed

        if start_h == end_h and start_m == end_m:
            return "开始时间和结束时间不能相同"

        curfew_mgr = self.curfew_managers.get(event.get_self_id())
        if not curfew_mgr:
            return "宵禁管理器未初始化"

        await curfew_mgr.enable_curfew(event.get_group_id(), start_str, end_str)
        return f"宵禁任务已创建：{start_str}~{end_str}"

    async def stop_curfew(self, event: AiocqhttpMessageEvent):
        curfew_mgr = self.curfew_managers.get(event.get_self_id())
        if not curfew_mgr:
            return "宵禁管理器未初始化"
        if await curfew_mgr.disable_curfew(event.get_group_id()):
            return "本群宵禁任务已取消"
        else:
            return "本群没有宵禁任务"

    async def stop_all_tasks(self):
        for _, curfew_mgr in self.curfew_managers.items():
            for cw in list(curfew_mgr.tasks.values()):
                cw.stop_curfew_task()
            curfew_mgr.tasks.clear()
        await self.store.save_async()
