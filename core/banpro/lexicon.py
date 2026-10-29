"""词表模块：内置/全局禁词文件 IO 与命中执法。"""

import json

from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from ...config import PluginConfig
from ...data import QQAdminDB
from ...utils import is_group_admin


class LexiconStore:
    """词表模块：内置/全局禁词文件 IO 与命中执法。"""

    def __init__(self, config: PluginConfig, db: QQAdminDB):
        self.cfg = config
        self.db = db
        self.builtin_ban_data = json.loads(config.ban_lexicon_path.read_text(encoding="utf-8"))
        self.builtin_ban_version = str(self.builtin_ban_data.get("lastUpdateDate", "未知"))
        self.builtin_ban_words = self._clean_words(self.builtin_ban_data.get("words", []))
        self.global_ban_words = self._load_global_ban_words()

    @staticmethod
    def _clean_words(words) -> list[str]:
        return list(dict.fromkeys(str(word).strip() for word in words if str(word).strip()))

    def _load_global_ban_words(self) -> list[str]:
        path = self.cfg.global_ban_lexicon_path
        if not path.exists():
            self._save_global_ban_words(self.builtin_ban_words)
            return list(self.builtin_ban_words)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return self._clean_words(data.get("words", []) if isinstance(data, dict) else data)
        except Exception as e:
            logger.warning(f"读取全局禁词失败，已回退内置禁词: {e}")
            return list(self.builtin_ban_words)

    def _save_global_ban_words(self, words: list[str]) -> None:
        self.global_ban_words = self._clean_words(words)
        self.cfg.global_ban_lexicon_path.write_text(
            json.dumps({"words": self.global_ban_words}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get_global_ban_words(self) -> list[str]:
        return list(self.global_ban_words)

    def get_available_builtin_ban_words(self) -> list[str]:
        global_words = set(self.global_ban_words)
        return [word for word in self.builtin_ban_words if word not in global_words]

    def set_global_ban_words(self, words: list[str]) -> list[str]:
        self._save_global_ban_words(words)
        return self.get_global_ban_words()

    def import_builtin_ban_words(self, words: list[str]) -> list[str]:
        builtin_words = set(self.builtin_ban_words)
        return self.set_global_ban_words(self.global_ban_words + [word for word in words if word in builtin_words])

    def restore_builtin_ban_words(self) -> list[str]:
        return self.set_global_ban_words(self.builtin_ban_words)

    @staticmethod
    def find_hit(message: str, ban_words: list[str], gid: str) -> str | None:
        """纯匹配：返回命中的第一条禁词；忽略空词与单字词（后者记 warning）。"""
        msg = message.lower()
        for word in ban_words:
            w = str(word).strip()
            if not w:
                continue
            if len(w) == 1:
                logger.warning(f"跳过单字禁词以避免过度匹配: {w!r} (群{gid})")
                continue
            if w.lower() in msg:
                return w
        return None

    async def on_ban_words(self, event: AiocqhttpMessageEvent):
        """检测禁词并撤回消息、禁言用户；管理/超管发言不处理。"""
        sender_id = str(event.get_sender_id())
        if sender_id in self.cfg.admins_id or is_group_admin(event):
            return

        gid = event.get_group_id()
        snapshot = self.db.get_group_snapshot(gid)

        # 检测自定义的违禁词
        if ban_words := snapshot.get("custom_ban_words", []):
            if await self.check_ban_words(event, ban_words):
                event.stop_event()
                return

        # 检测内置违禁词
        if snapshot.get("builtin_ban", False):
            if await self.check_ban_words(event, self.global_ban_words):
                event.stop_event()
                return

    async def check_ban_words(self, event: AiocqhttpMessageEvent, ban_words: list[str]) -> bool:
        """检测违禁词并撤回消息、禁言发送者；命中返回 True。"""
        gid = event.get_group_id()
        if self.find_hit(event.message_str, ban_words, gid) is None:
            return False
        # 撤回消息
        try:
            message_id = event.message_obj.message_id
            await event.bot.delete_msg(message_id=int(message_id))
        except Exception:
            pass
        # 禁言发送者
        ban_time = await self.db.get(gid, "word_ban_time", 0)
        if ban_time > 0:
            try:
                await event.bot.set_group_ban(
                    group_id=int(event.get_group_id()),
                    user_id=int(event.get_sender_id()),
                    duration=ban_time,
                )
            except Exception:
                logger.error(f"bot在群{event.get_group_id()}权限不足，禁言失败")
                pass
        return True
