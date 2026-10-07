"""词表模块：内置/全局禁词文件 IO、违禁词配置命令与命中执法。"""

import json

from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from ...config import PluginConfig
from ...data import QQAdminDB
from ...utils import parse_bool


class LexiconStore:
    """词表模块：内置/全局禁词文件 IO、违禁词配置命令与命中执法。"""

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

    async def handle_word_ban_time(self, event: AiocqhttpMessageEvent, time: int | None):
        """设置禁词禁言时长"""
        gid = event.get_group_id()
        if isinstance(time, int):
            await self.db.set(gid, "word_ban_time", time)
            msg = f"本群禁词禁言时长已设为：{time} 秒" if time > 0 else "本群禁词禁言已关闭"
            await event.send(event.plain_result(msg))
        else:
            status = await self.db.get(gid, "word_ban_time", 0)
            await event.send(event.plain_result(f"本群禁词禁言时长：{status} 秒"))

    async def handle_ban_words(self, event: AiocqhttpMessageEvent):
        """设置/查看违禁词"""
        gid = event.get_group_id()
        raw = event.message_str.partition(" ")[2]

        # 1. 空指令：查看
        if not raw:
            words = await self.db.get(gid, "custom_ban_words", [])
            await event.send(event.plain_result(f"本群违禁词：{words}"))
            return

        # 2. 纯单词列表（无 +/-）：整表覆写
        toks = raw.split()
        if all(not tok.startswith(("+", "-")) for tok in toks):
            await self.db.set(gid, "custom_ban_words", toks)
            await event.send(event.plain_result(f"本群违禁词已覆写为：{' '.join(toks)}"))
            return

        # 3. 增量模式：+word / -word
        curr = set(await self.db.get(gid, "custom_ban_words", []))
        added, removed = [], []

        for tok in toks:
            if tok.startswith("+") and len(tok) > 1:
                w = tok[1:]
                if w not in curr:
                    curr.add(w)
                    added.append(w)
            elif tok.startswith("-") and len(tok) > 1:
                w = tok[1:]
                if w in curr:
                    curr.discard(w)
                    removed.append(w)

        await self.db.set(gid, "custom_ban_words", list(curr))

        reply = ["本群违禁词"]
        if added:
            reply.append(f"新增：{'、'.join(added)}")
        if removed:
            reply.append(f"移除：{'、'.join(removed)}")
        if not added and not removed:
            reply.append("无变动")
        await event.send(event.plain_result("\n".join(reply)))

    async def handle_builtin_ban_words(self, event: AiocqhttpMessageEvent, mode_str: str | bool | None):
        """启用/停用全局违禁词"""
        gid = event.get_group_id()
        mode = parse_bool(mode_str)

        if isinstance(mode, bool):
            await self.db.set(gid, "builtin_ban", mode)
            await event.send(event.plain_result(f"本群全局禁词：{mode}"))
        else:
            status = await self.db.get(gid, "builtin_ban", False)
            await event.send(event.plain_result(f"本群全局禁词：{status}"))

    async def on_ban_words(self, event: AiocqhttpMessageEvent):
        """检测禁词并撤回消息、禁言用户"""
        gid = event.get_group_id()
        snapshot = self.db.get_group_snapshot(gid)

        # 检测自定义的违禁词
        if ban_words := snapshot.get("custom_ban_words", []):
            if await self.check_ban_words(event, ban_words):
                return

        # 检测内置违禁词
        if snapshot.get("builtin_ban", False):
            if await self.check_ban_words(event, self.global_ban_words):
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
