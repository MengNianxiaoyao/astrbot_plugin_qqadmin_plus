"""宵禁门面：多 Bot 总管（初始化/命令入口/时间解析/全局启停）。

子模块：curfew_store.CurfewStore / group.GroupCurfew / curfew_manager.BotCurfewManager。
新人找定时逻辑去 group，找多群管理去 manager，这里只做装配与命令入口。
"""

import asyncio

from aiocqhttp import Event
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from astrbot.api import logger
from astrbot.api.star import Context
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (
    AiocqhttpAdapter,
)

from ...config import PluginConfig, get_plugin_timezone
from ...group_cache import iter_aiocqhttp_adapters
from ...utils import parse_clock
from .curfew_manager import BotCurfewManager
from .curfew_store import CurfewStore

__all__ = ["BotCurfewManager", "CurfewHandle", "CurfewStore"]


class CurfewHandle:
    """多 Bot 宵禁处理类"""

    def __init__(self, context: Context, config: PluginConfig):
        self.context = context
        self.timezone = get_plugin_timezone(self.context)
        self.scheduler = AsyncIOScheduler(timezone=self.timezone)
        self.scheduler.start()
        self.store = CurfewStore(config.curfew_file)
        self.store.load()
        self.curfew_managers: dict[str, BotCurfewManager] = {}
        self._init_lock = asyncio.Lock()

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

        if bot_id in self.curfew_managers:
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
        async with self._init_lock:
            tasks = [self._initialize_aiocqhttp_adapter(inst) for inst in iter_aiocqhttp_adapters(self.context)]
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for r in results:
                    if isinstance(r, Exception):
                        logger.error(f"宵禁初始化子任务异常: {r}")

    async def start_curfew(
        self,
        event: AiocqhttpMessageEvent,
        input_start_time: str | None = None,
        input_end_time: str | None = None,
    ):
        if not input_start_time or not input_end_time:
            return "未输入范围 HH:MM HH:MM"

        start = parse_clock(input_start_time)
        end = parse_clock(input_end_time)
        if not start or not end:
            return "时间格式错误，应为 HH:MM"
        if start == end:
            return "开始时间和结束时间不能相同"

        # 统一存成 HH:MM，兼容命令里写成 9:5 或中文冒号
        start_str = f"{start[0]:02d}:{start[1]:02d}"
        end_str = f"{end[0]:02d}:{end[1]:02d}"

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
        for curfew_mgr in self.curfew_managers.values():
            for cw in list(curfew_mgr.tasks.values()):
                cw.stop_curfew_task()
            curfew_mgr.tasks.clear()
        try:
            if self.scheduler.running:
                self.scheduler.shutdown(wait=False)
        except Exception as e:
            logger.debug(f"关闭宵禁调度器失败（可能已关闭）: {e}")
        await self.store.save_async()
