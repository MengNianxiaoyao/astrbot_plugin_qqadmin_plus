from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from ...config import PluginConfig
from ...data import QQAdminDB, QQAdminGlobalList
from ...utils import parse_bool


class JoinManager:
    """进群配置管理命令：审核开关、关键词、名单、等级、欢迎语等。"""

    def __init__(
        self,
        config: PluginConfig,
        db: QQAdminDB,
        global_list: QQAdminGlobalList,
    ):
        self.cfg = config
        self.db = db
        self.global_list = global_list

    async def handle_join_review(self, event: AiocqhttpMessageEvent, mode_str: str | bool | None):
        gid = event.get_group_id()
        mode = parse_bool(mode_str)
        if isinstance(mode, bool):
            await self.db.set(gid, "join_switch", mode)
            await event.send(event.plain_result(f"本群进群审核：{mode}"))
        else:
            status = await self.db.get(gid, "join_switch")
            await event.send(event.plain_result(f"本群进群审核：{status}"))

    async def handle_accept_words(self, event: AiocqhttpMessageEvent):
        gid = event.get_group_id()
        raw = event.message_str.partition(" ")[2]
        if raw:
            words = raw.split()
            await self.db.set(gid, "join_accept_words", words)
            await event.send(event.plain_result(f"本群进群白词已设为：{words}"))
        else:
            words = await self.db.get(gid, "join_accept_words", [])
            await event.send(event.plain_result(f"本群进群白词：{words}"))

    async def handle_reject_words(self, event: AiocqhttpMessageEvent):
        gid = event.get_group_id()
        raw = event.message_str.partition(" ")[2]
        if raw:
            words = raw.split()
            await self.db.set(gid, "join_reject_words", words)
            await event.send(event.plain_result(f"本群进群黑词已设为：{words}"))
        else:
            words = await self.db.get(gid, "join_reject_words", [])
            await event.send(event.plain_result(f"本群进群黑词：{words}"))

    async def handle_no_match_reject(self, event: AiocqhttpMessageEvent, mode_str: str | bool | None):
        gid = event.get_group_id()
        mode = parse_bool(mode_str)
        if isinstance(mode, bool):
            await self.db.set(gid, "join_no_match_reject", mode)
            await event.send(event.plain_result(f"本群未命中白词驳回已设为：{mode}"))
        else:
            status = await self.db.get(gid, "join_no_match_reject")
            await event.send(event.plain_result(f"本群未命中白词驳回：{status}"))

    async def handle_join_full_reject(self, event: AiocqhttpMessageEvent, mode_str: str | bool | None):
        gid = event.get_group_id()
        mode = parse_bool(mode_str)
        if isinstance(mode, bool):
            await self.db.set(gid, "join_full_reject", mode)
            await event.send(event.plain_result(f"本群群满自动驳回已设为：{mode}"))
        else:
            status = await self.db.get(gid, "join_full_reject", True)
            await event.send(event.plain_result(f"本群群满自动驳回：{status}"))

    async def handle_join_full_msg(self, event: AiocqhttpMessageEvent):
        gid = event.get_group_id()
        raw = event.message_str.partition(" ")[2]

        if raw:
            await self.db.set(gid, "join_full_msg", raw)
            await event.send(event.plain_result(f"本群群满拒绝文案已设为：\n{raw}"))
        else:
            text = await self.db.get(gid, "join_full_msg", "群人数已满")
            await event.send(event.plain_result(f"本群群满拒绝文案：\n{text or '（未设置）'}"))

    async def handle_join_min_level(self, event: AiocqhttpMessageEvent, level: int | None):
        gid = event.get_group_id()
        if isinstance(level, int):
            await self.db.set(gid, "join_min_level", level)
            msg = f"本群进群等级门槛已设为：{level} 级" if level > 0 else "已解除本群的进群等级限制"
            await event.send(event.plain_result(msg))
        else:
            level = await self.db.get(gid, "join_min_level", 8)
            await event.send(event.plain_result(f"本群进群等级门槛: {level} 级"))

    async def handle_join_max_time(self, event: AiocqhttpMessageEvent, time: int | None):
        gid = event.get_group_id()
        if isinstance(time, int):
            await self.db.set(gid, "join_max_time", time)
            msg = f"本群进群次数已限制为：{time} 次" if time > 0 else "已解除本群的进群次数限制"
            await event.send(event.plain_result(msg))
        else:
            time = await self.db.get(gid, "join_max_time")
            await event.send(event.plain_result(f"本群进群可尝试次数：{time} 次"))

    async def _handle_id_list(self, event: AiocqhttpMessageEvent, field: str, label: str, global_mode: bool = False):
        raw = event.message_str.partition(" ")[2]
        gid = event.get_group_id()
        prefix = "全局" if global_mode else "本群进群"

        if global_mode:
            list_type = field.removesuffix("_ids")
            lst = self.global_list.get(list_type)
        else:
            lst = await self.db.get(gid, field, [])

        if not raw:
            await event.send(event.plain_result(f"{prefix}{label}：{lst}"))
            return

        if all(tok.isdigit() for tok in raw.split()):
            new_ids = raw.split()
            if global_mode:
                self.global_list.set(list_type, new_ids)
            else:
                await self.db.set(gid, field, new_ids)
            await event.send(event.plain_result(f"{prefix}{label}已覆写为：{' '.join(new_ids)}"))
            return

        curr = set(lst)
        added, removed = [], []
        for tok in raw.split():
            if tok.startswith("+") and tok[1:].isdigit():
                uid = tok[1:]
                if uid not in curr:
                    curr.add(uid)
                    added.append(uid)
            elif tok.startswith("-") and tok[1:].isdigit():
                uid = tok[1:]
                if uid in curr:
                    curr.discard(uid)
                    removed.append(uid)

        result = list(curr)
        if global_mode:
            self.global_list.set(list_type, result)
        else:
            await self.db.set(gid, field, result)

        reply = [f"{prefix}{label}"]
        if added:
            reply.append(f"新增：{'、'.join(added)}")
        if removed:
            reply.append(f"移除：{'、'.join(removed)}")
        if not added and not removed:
            reply.append("无变动")
        await event.send(event.plain_result("\n".join(reply)))

    async def handle_allow_ids(self, event: AiocqhttpMessageEvent):
        await self._handle_id_list(event, "allow_ids", "白名单")

    async def handle_block_ids(self, event: AiocqhttpMessageEvent):
        await self._handle_id_list(event, "block_ids", "黑名单")

    async def handle_global_allow(self, event: AiocqhttpMessageEvent):
        await self._handle_id_list(event, "allow_ids", "白名单", global_mode=True)

    async def handle_global_block(self, event: AiocqhttpMessageEvent):
        await self._handle_id_list(event, "block_ids", "黑名单", global_mode=True)

    async def handle_join_ban(self, event: AiocqhttpMessageEvent, time: int | None):
        gid = event.get_group_id()
        if isinstance(time, int):
            await self.db.set(gid, "join_ban_time", time)
            msg = f"本群进群禁言已设为：{time} 秒" if time > 0 else "已关闭本群进群禁言"
            await event.send(event.plain_result(msg))
        else:
            t = await self.db.get(gid, "join_ban_time", 0)
            await event.send(event.plain_result(f"本群进群禁言设置：{t} 秒"))

    async def handle_join_welcome(self, event: AiocqhttpMessageEvent):
        gid = event.get_group_id()
        raw = event.message_str.partition(" ")[2]

        if raw:
            await self.db.set(gid, "join_welcome", raw)
            await event.send(event.plain_result(f"本群进群欢迎语已设为：\n{raw}"))
        else:
            text = await self.db.get(gid, "join_welcome", "")
            await event.send(event.plain_result(f"本群进群欢迎语：\n{text or '（未设置）'}"))

    async def handle_leave_notify(self, event: AiocqhttpMessageEvent, mode_str):
        gid = event.get_group_id()
        mode = parse_bool(mode_str)
        if isinstance(mode, bool):
            await self.db.set(gid, "leave_notify", mode)
            await event.send(event.plain_result(f"本群退群通知已设为：{mode}"))
        else:
            status = await self.db.get(gid, "leave_notify")
            await event.send(event.plain_result(f"本群退群通知：{status}"))

    async def handle_leave_block(self, event: AiocqhttpMessageEvent, mode_str):
        gid = event.get_group_id()
        mode = parse_bool(mode_str)
        if isinstance(mode, bool):
            await self.db.set(gid, "leave_block", mode)
            await event.send(event.plain_result(f"本群退群拉黑已设为：{mode}"))
        else:
            status = await self.db.get(gid, "leave_block")
            await event.send(event.plain_result(f"本群退群拉黑：{status}"))
