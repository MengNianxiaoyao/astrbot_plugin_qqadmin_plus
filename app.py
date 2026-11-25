"""应用装配根：组装配置、存储、平台缓存、业务能力、权限与 Web 面板。

``main.py`` 只负责 AstrBot 的 Handler 注册与转发；所有对象在这里创建，
生命周期（初始化/停止/后台任务）也收敛在这里，避免框架层与业务层互相缠绕。
"""

import asyncio

from astrbot.api import AstrBotConfig
from astrbot.api.star import Context

from .config import PluginConfig
from .core import (
    BanproHandle,
    CurfewHandle,
    FileHandle,
    JoinHandle,
    LotteryHandle,
    MemberHandle,
    NormalHandle,
    NoticeHandle,
    RecallHandle,
)
from .data import QQAdminDB, QQAdminGlobalList
from .group_cache import QQGroupInfoCache
from .permission import PermissionManager
from .utils import BackgroundTasks
from .web import QQAdminWebController


class QQAdminApp:
    """插件运行时容器，持有全部业务对象与后台任务。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        self.context = context
        self.cfg = PluginConfig(config, context)
        self.db = QQAdminDB(self.cfg)
        self.db.default_cfg = self.cfg.build_group_default_config()
        self.group_cache = QQGroupInfoCache(context, self.db)
        self.global_list = QQAdminGlobalList(self.cfg.data_dir)
        self.permissions = PermissionManager(self.cfg, self.db)

        self.normal = NormalHandle(self.cfg, self.db, self.global_list)
        self.recall = RecallHandle()
        self.notice = NoticeHandle(self.cfg)
        self.banpro = BanproHandle(self.cfg, self.db, self.permissions)
        self.join = JoinHandle(self.cfg, self.db, self.global_list, self.group_cache)
        self.member = MemberHandle(self.db, self.global_list)
        self.file = FileHandle(self.cfg)
        self.curfew = CurfewHandle(context, self.cfg)
        self.lottery = LotteryHandle(context, self.cfg, self.group_cache)
        self.web = QQAdminWebController(
            context,
            self.cfg,
            self.db,
            self.group_cache,
            self.global_list,
            self.banpro,
            self.permissions,
            self.join.state,
        )

        self._background = BackgroundTasks("后台任务")

    # ---------- 生命周期 ----------

    def register_web_api(self) -> None:
        self.web.register_routes()

    async def start(self) -> None:
        """初始化存储，并恢复依赖平台连接的定时任务。"""
        await self.db.init()
        self.spawn(self.curfew.initialize())
        self.spawn(self.lottery.initialize())

    async def stop(self) -> None:
        self.banpro.stop()
        await self.lottery.stop()
        await self.curfew.stop_all_tasks()
        await self._cancel_bg_tasks()
        await self.db.close()

    def on_platform_loaded(self) -> None:
        """平台（重）加载后补齐宵禁与抽奖任务，initialize 内部幂等。"""
        self.spawn(self.curfew.initialize())
        self.spawn(self.lottery.initialize())

    # ---------- 后台任务 ----------

    def spawn(self, coro) -> None:
        """保留后台任务引用，避免被回收，并统一记录异常。"""
        self._background.spawn(coro)

    async def _cancel_bg_tasks(self) -> None:
        tasks = self._background.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
