from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from ...data import QQAdminDB, QQAdminGlobalList
from ...utils import parse_bool


@dataclass(frozen=True)
class FieldSpec:
    """get/set 型配置命令的描述：一行代替一个近乎相同的方法体。"""

    field: str
    kind: str  # "bool" | "words" | "text" | "int"
    default: Any = None
    set_tpl: str = ""
    show_tpl: str = ""
    zero_tpl: str | None = None  # int-kind：值为 0 时的设置文案
    empty_label: str = "（未设置）"  # text-kind：空值展示


FIELD_SPECS: dict[str, FieldSpec] = {
    "handle_join_review": FieldSpec("join_switch", "bool", None, "本群进群审核：{v}", "本群进群审核：{v}"),
    "handle_no_match_reject": FieldSpec("join_no_match_reject", "bool", None, "本群未命中白词驳回已设为：{v}", "本群未命中白词驳回：{v}"),
    "handle_join_full_reject": FieldSpec("join_full_reject", "bool", True, "本群群满自动驳回已设为：{v}", "本群群满自动驳回：{v}"),
    "handle_leave_notify": FieldSpec("leave_notify", "bool", None, "本群退群通知已设为：{v}", "本群退群通知：{v}"),
    "handle_leave_block": FieldSpec("leave_block", "bool", None, "本群退群拉黑已设为：{v}", "本群退群拉黑：{v}"),
    "handle_accept_words": FieldSpec("join_accept_words", "words", [], "本群进群白词已设为：{v}", "本群进群白词：{v}"),
    "handle_reject_words": FieldSpec("join_reject_words", "words", [], "本群进群黑词已设为：{v}", "本群进群黑词：{v}"),
    "handle_join_full_msg": FieldSpec("join_full_msg", "text", "群人数已满", "本群群满拒绝文案已设为：\n{v}", "本群群满拒绝文案：\n{v}"),
    "handle_join_welcome": FieldSpec("join_welcome", "text", "", "本群进群欢迎语已设为：\n{v}", "本群进群欢迎语：\n{v}"),
    "handle_join_min_level": FieldSpec("join_min_level", "int", 8, "本群进群等级门槛已设为：{v} 级", "本群进群等级门槛: {v} 级", "已解除本群的进群等级限制"),
    "handle_join_max_time": FieldSpec("join_max_time", "int", None, "本群进群次数已限制为：{v} 次", "本群进群可尝试次数：{v} 次", "已解除本群的进群次数限制"),
    "handle_join_ban": FieldSpec("join_ban_time", "int", 0, "本群进群禁言已设为：{v} 秒", "本群进群禁言设置：{v} 秒", "已关闭本群进群禁言"),
}


async def _run_bool(mgr: JoinManager, event: AiocqhttpMessageEvent, spec: FieldSpec, mode_str: str | bool | None):
    gid = event.get_group_id()
    mode = parse_bool(mode_str)
    if isinstance(mode, bool):
        await mgr.db.set(gid, spec.field, mode)
        await event.send(event.plain_result(spec.set_tpl.format(v=mode)))
    else:
        status = await mgr.db.get(gid, spec.field, spec.default)
        await event.send(event.plain_result(spec.show_tpl.format(v=status)))


async def _run_words(mgr: JoinManager, event: AiocqhttpMessageEvent, spec: FieldSpec):
    gid = event.get_group_id()
    raw = event.message_str.partition(" ")[2]
    if raw:
        words = raw.split()
        await mgr.db.set(gid, spec.field, words)
        await event.send(event.plain_result(spec.set_tpl.format(v=words)))
    else:
        words = await mgr.db.get(gid, spec.field, spec.default)
        await event.send(event.plain_result(spec.show_tpl.format(v=words)))


async def _run_text(mgr: JoinManager, event: AiocqhttpMessageEvent, spec: FieldSpec):
    gid = event.get_group_id()
    raw = event.message_str.partition(" ")[2]
    if raw:
        await mgr.db.set(gid, spec.field, raw)
        await event.send(event.plain_result(spec.set_tpl.format(v=raw)))
    else:
        text = await mgr.db.get(gid, spec.field, spec.default)
        await event.send(event.plain_result(spec.show_tpl.format(v=text or spec.empty_label)))


async def _run_int(mgr: JoinManager, event: AiocqhttpMessageEvent, spec: FieldSpec, value: int | None):
    gid = event.get_group_id()
    if isinstance(value, int):
        await mgr.db.set(gid, spec.field, value)
        msg = spec.set_tpl.format(v=value) if value > 0 else spec.zero_tpl
        await event.send(event.plain_result(msg))
    else:
        current = await mgr.db.get(gid, spec.field, spec.default)
        await event.send(event.plain_result(spec.show_tpl.format(v=current)))


class JoinManager:
    """进群配置管理命令：审核开关、关键词、名单、等级、欢迎语等。"""

    def __init__(
        self,
        db: QQAdminDB,
        global_list: QQAdminGlobalList,
    ):
        self.db = db
        self.global_list = global_list

    async def handle_join_review(self, event: AiocqhttpMessageEvent, mode_str: str | bool | None):
        await _run_bool(self, event, FIELD_SPECS["handle_join_review"], mode_str)

    async def handle_accept_words(self, event: AiocqhttpMessageEvent):
        await _run_words(self, event, FIELD_SPECS["handle_accept_words"])

    async def handle_reject_words(self, event: AiocqhttpMessageEvent):
        await _run_words(self, event, FIELD_SPECS["handle_reject_words"])

    async def handle_no_match_reject(self, event: AiocqhttpMessageEvent, mode_str: str | bool | None):
        await _run_bool(self, event, FIELD_SPECS["handle_no_match_reject"], mode_str)

    async def handle_join_full_reject(self, event: AiocqhttpMessageEvent, mode_str: str | bool | None):
        await _run_bool(self, event, FIELD_SPECS["handle_join_full_reject"], mode_str)

    async def handle_join_full_msg(self, event: AiocqhttpMessageEvent):
        await _run_text(self, event, FIELD_SPECS["handle_join_full_msg"])

    async def handle_join_min_level(self, event: AiocqhttpMessageEvent, level: int | None):
        await _run_int(self, event, FIELD_SPECS["handle_join_min_level"], level)

    async def handle_join_max_time(self, event: AiocqhttpMessageEvent, time: int | None):
        await _run_int(self, event, FIELD_SPECS["handle_join_max_time"], time)

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
        await _run_int(self, event, FIELD_SPECS["handle_join_ban"], time)

    async def handle_join_welcome(self, event: AiocqhttpMessageEvent):
        await _run_text(self, event, FIELD_SPECS["handle_join_welcome"])

    async def handle_leave_notify(self, event: AiocqhttpMessageEvent, mode_str):
        await _run_bool(self, event, FIELD_SPECS["handle_leave_notify"], mode_str)

    async def handle_leave_block(self, event: AiocqhttpMessageEvent, mode_str):
        await _run_bool(self, event, FIELD_SPECS["handle_leave_block"], mode_str)
