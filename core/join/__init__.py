from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from ...config import PluginConfig
from ...data import QQAdminDB, QQAdminGlobalList
from .approve import JoinApprover
from .events import JoinEvents
from .manage import JoinManager
from .review import JoinReviewer
from .state import JoinState


class JoinHandle:
    """进群模块门面：对外 API 与拆分前完全一致，内部按审核判定/配置管理/人工审批/事件监听分工。"""

    def __init__(
        self,
        config: PluginConfig,
        db: QQAdminDB,
        global_list: QQAdminGlobalList,
        group_cache,
    ):
        self.cfg = config
        self.db = db
        self.global_list = global_list
        self._group_cache = group_cache
        self.state = JoinState()
        self.reviewer = JoinReviewer(db, global_list, group_cache, self.state)
        self.manager = JoinManager(config, db, global_list)
        self.approver = JoinApprover(self.state)
        self.events = JoinEvents(config, db, global_list, group_cache, self.state, self.reviewer)

    # -----------修改配置-----------------

    async def handle_join_review(self, event: AiocqhttpMessageEvent, mode_str: str | bool | None):
        await self.manager.handle_join_review(event, mode_str)

    async def handle_accept_words(self, event: AiocqhttpMessageEvent):
        await self.manager.handle_accept_words(event)

    async def handle_reject_words(self, event: AiocqhttpMessageEvent):
        await self.manager.handle_reject_words(event)

    async def handle_no_match_reject(self, event: AiocqhttpMessageEvent, mode_str: str | bool | None):
        await self.manager.handle_no_match_reject(event, mode_str)

    async def handle_join_full_reject(self, event: AiocqhttpMessageEvent, mode_str: str | bool | None):
        await self.manager.handle_join_full_reject(event, mode_str)

    async def handle_join_full_msg(self, event: AiocqhttpMessageEvent):
        await self.manager.handle_join_full_msg(event)

    async def handle_join_min_level(self, event: AiocqhttpMessageEvent, level: int | None):
        await self.manager.handle_join_min_level(event, level)

    async def handle_join_max_time(self, event: AiocqhttpMessageEvent, time: int | None):
        await self.manager.handle_join_max_time(event, time)

    async def handle_allow_ids(self, event: AiocqhttpMessageEvent):
        await self.manager.handle_allow_ids(event)

    async def handle_block_ids(self, event: AiocqhttpMessageEvent):
        await self.manager.handle_block_ids(event)

    async def handle_global_allow(self, event: AiocqhttpMessageEvent):
        await self.manager.handle_global_allow(event)

    async def handle_global_block(self, event: AiocqhttpMessageEvent):
        await self.manager.handle_global_block(event)

    async def handle_join_ban(self, event: AiocqhttpMessageEvent, time: int | None):
        await self.manager.handle_join_ban(event, time)

    async def handle_join_welcome(self, event: AiocqhttpMessageEvent):
        await self.manager.handle_join_welcome(event)

    async def handle_leave_notify(self, event: AiocqhttpMessageEvent, mode_str):
        await self.manager.handle_leave_notify(event, mode_str)

    async def handle_leave_block(self, event: AiocqhttpMessageEvent, mode_str):
        await self.manager.handle_leave_block(event, mode_str)

    # -----------审核判定-----------------

    async def should_approve(
        self,
        gid: str,
        uid: str,
        comment: str | None = None,
        user_level: int | None = None,
        client=None,
    ) -> tuple[bool | None, str, str]:
        return await self.reviewer.should_approve(gid, uid, comment, user_level, client)

    # ---------处理事件-----------------

    async def event_monitoring(self, event: AiocqhttpMessageEvent):
        """监听进群/退群事件"""
        await self.events.event_monitoring(event)

    # ---------人工审批-----------------

    async def set_approve(self, event: AiocqhttpMessageEvent, extra: str = "", approve: bool = True) -> str | None:
        return await self.approver.set_approve(event, extra, approve)

    async def agree_add_group(self, event: AiocqhttpMessageEvent, extra: str = ""):
        """批准进群申请"""
        await self.approver.agree_add_group(event, extra)

    async def refuse_add_group(self, event: AiocqhttpMessageEvent, extra: str = ""):
        """驳回进群申请"""
        await self.approver.refuse_add_group(event, extra)


__all__ = ["JoinHandle"]
