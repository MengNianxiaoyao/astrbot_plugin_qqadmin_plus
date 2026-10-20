from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from ...config import PluginConfig
from ...data import QQAdminDB, QQAdminGlobalList
from .approve import JoinApprover
from .events import JoinEvents
from .review import JoinReviewer
from .state import JoinState


class JoinHandle:
    """进群模块门面：审核判定 / 人工审批 / 事件监听分工。"""

    def __init__(
        self,
        config: PluginConfig,
        db: QQAdminDB,
        global_list: QQAdminGlobalList,
        group_cache,
    ):
        self.state = JoinState(config.data_dir)
        self.reviewer = JoinReviewer(db, global_list, group_cache, self.state)
        self.approver = JoinApprover(self.state)
        self.events = JoinEvents(config, db, global_list, group_cache, self.state, self.reviewer)

    # ---------处理事件-----------------

    async def event_monitoring(self, event: AiocqhttpMessageEvent):
        """监听进群/退群事件"""
        await self.events.event_monitoring(event)

    # ---------人工审批-----------------

    async def agree_add_group(self, event: AiocqhttpMessageEvent, extra: str = ""):
        """批准进群申请"""
        await self.approver.agree_add_group(event, extra)

    async def refuse_add_group(self, event: AiocqhttpMessageEvent, extra: str = ""):
        """驳回进群申请"""
        await self.approver.refuse_add_group(event, extra)


__all__ = ["JoinHandle"]
