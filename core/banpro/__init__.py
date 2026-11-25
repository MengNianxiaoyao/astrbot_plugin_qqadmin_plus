"""内容治理门面：违禁词、刷屏与投票，内部按子模块分工。

子模块：lexicon.LexiconStore / spam.SpamDetector / vote.VoteSession。
新人找逻辑去子模块；这里只做聚合转发。
"""

from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from ...config import PluginConfig
from ...data import QQAdminDB
from ...permission.manager import PermissionManager
from .lexicon import LexiconStore
from .spam import SpamDetector
from .vote import VoteSession

__all__ = ["BanproHandle", "LexiconStore", "SpamDetector", "VoteSession"]


class BanproHandle:
    """内容治理门面：违禁词/刷屏/投票对外入口，内部按子模块分工。"""

    def __init__(self, config: PluginConfig, db: QQAdminDB, permissions: PermissionManager):
        self.cfg = config
        self.db = db
        self.lexicon = LexiconStore(config, db)
        self.spam = SpamDetector(config, db)
        self.votes = VoteSession(config, db, permissions)

    @property
    def builtin_ban_version(self) -> str:
        return self.lexicon.builtin_ban_version

    # -----------词表-----------------

    def get_global_ban_words(self) -> list[str]:
        return self.lexicon.get_global_ban_words()

    def get_available_builtin_ban_words(self) -> list[str]:
        return self.lexicon.get_available_builtin_ban_words()

    def set_global_ban_words(self, words: list[str]) -> list[str]:
        return self.lexicon.set_global_ban_words(words)

    def import_builtin_ban_words(self, words: list[str]) -> list[str]:
        return self.lexicon.import_builtin_ban_words(words)

    def restore_builtin_ban_words(self) -> list[str]:
        return self.lexicon.restore_builtin_ban_words()

    async def on_ban_words(self, event: AiocqhttpMessageEvent):
        await self.lexicon.on_ban_words(event)

    # -----------刷屏-----------------

    async def spamming_ban(self, event: AiocqhttpMessageEvent):
        await self.spam.spamming_ban(event)

    # -----------生命周期-----------------

    def stop(self):
        """停止后台投票定时任务。"""
        self.votes.stop()

    # -----------投票-----------------

    async def start_vote_mute(self, event, ban_time: int | None = None) -> str | None:
        return await self.votes.start_vote_mute(event, ban_time)

    async def start_vote_kick(self, event) -> str | None:
        return await self.votes.start_vote_kick(event)

    async def cancel_vote_mute(self, event: AiocqhttpMessageEvent) -> str | None:
        return await self.votes.cancel_vote_mute(event)

    async def vote_mute(self, event: AiocqhttpMessageEvent, agree: bool) -> str | None:
        return await self.votes.vote_mute(event, agree)
