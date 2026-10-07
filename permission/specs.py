"""命令权限元数据：全仓唯一出处（single source of truth）。

新人读这三行就够：
- 一条命令一行：只写 perm_key（_conf_schema.json 的权限键）；Bot 等级
  默认 ADMIN，只有 OWNER/MEMBER 才显式写；check_at/allow_private 同理。
- 带 llm_tool 的行自动成为 LLM 工具门禁——LLM 的 perm_key/bot_perm 与
  命令共用同一行，不再维护第二张表（之前逐项重复是主要复杂度来源）。
- main.py 只用两个工厂：perm_required_for（群聊命令）/ llm_guarded_for（LLM）。

Note: `handle_spamming_ban_time` intentionally uses
`perm_key="handle_builtin_ban_words"`. That is the schema's key for the
刷屏禁言指令 (`_conf_schema.json`), not copy-paste drift. It is pinned here
so the next reader doesn't "fix" it.
"""

from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from functools import wraps
from typing import Any

from .levels import PermLevel
from .manager import perm_manager, perm_required


@dataclass(frozen=True)
class CommandSpec:
    perm_key: str
    bot_perm: PermLevel = PermLevel.ADMIN
    check_at: bool = True
    allow_private: bool = False
    llm_tool: str | None = None


# Keyed by command method name in QQAdminPlugin. set_config / reset_config
# keep bespoke gates and stay out of this table.
COMMAND_SPECS: dict[str, CommandSpec] = {
    # ---------- 基础群管 ----------
    "set_group_ban": CommandSpec("group_ban", llm_tool="llm_set_group_ban"),
    "cancel_group_ban": CommandSpec("group_ban"),
    "set_group_whole_ban": CommandSpec("whole_ban", llm_tool="llm_set_group_whole_ban"),
    "set_group_card": CommandSpec("set_group_card", llm_tool="llm_set_group_card"),
    "set_group_special_title": CommandSpec("set_group_special_title", PermLevel.OWNER, llm_tool="llm_set_group_special_title"),
    "set_group_special_title_me": CommandSpec("set_group_special_title_me", PermLevel.OWNER),
    "set_group_kick": CommandSpec("set_group_kick", llm_tool="llm_set_group_kick"),
    "set_group_block": CommandSpec("set_group_block", llm_tool="llm_set_group_block"),
    "set_group_admin": CommandSpec("admin", PermLevel.OWNER, check_at=False),
    "cancel_group_admin": CommandSpec("admin", PermLevel.OWNER, check_at=False),
    "set_essence_msg": CommandSpec("essence", llm_tool="llm_set_essence_msg"),
    "delete_essence_msg": CommandSpec("essence"),
    "get_essence_msg_list": CommandSpec("get_essence_msg_list", llm_tool="llm_get_essence_msg_list"),
    "set_group_portrait": CommandSpec("set_group_portrait", llm_tool="llm_set_group_portrait"),
    "set_group_name": CommandSpec("set_group_name", llm_tool="llm_set_group_name"),
    "delete_msg": CommandSpec("delete_msg", PermLevel.MEMBER),
    "send_group_notice": CommandSpec("send_group_notice", llm_tool="llm_send_group_notice"),
    "get_group_notice": CommandSpec("get_group_notice", PermLevel.MEMBER, llm_tool="llm_get_group_notice"),
    # ---------- 违禁词 / 刷屏 / 投票 / 宵禁 ----------
    "handle_word_ban_time": CommandSpec("word_ban"),
    "handle_ban_words": CommandSpec("word_ban"),
    "handle_builtin_ban_words": CommandSpec("word_ban"),
    "handle_spamming_ban_time": CommandSpec("handle_builtin_ban_words"),
    "start_vote_mute": CommandSpec("vote"),
    "agree_vote_mute": CommandSpec("vote"),
    "disagree_vote_mute": CommandSpec("vote"),
    "cancel_vote_mute": CommandSpec("vote"),
    "start_vote_kick": CommandSpec("vote"),
    "agree_vote_kick": CommandSpec("vote"),
    "disagree_vote_kick": CommandSpec("vote"),
    "start_curfew": CommandSpec("curfew"),
    "stop_curfew": CommandSpec("curfew"),
    # ---------- 进群审核 / 欢迎 / 退群 ----------
    "handle_join_review": CommandSpec("join"),
    "handle_accept_words": CommandSpec("join"),
    "handle_reject_words": CommandSpec("join"),
    "handle_no_match_reject": CommandSpec("join"),
    "handle_join_full_reject": CommandSpec("join"),
    "handle_join_full_msg": CommandSpec("join"),
    "handle_join_min_level": CommandSpec("join"),
    "handle_join_max_time": CommandSpec("join"),
    "handle_allow_ids": CommandSpec("join"),
    "handle_reject_ids": CommandSpec("join"),
    "agree_add_group": CommandSpec("approve", allow_private=True),
    "refuse_add_group": CommandSpec("approve", allow_private=True),
    "handle_join_ban": CommandSpec("welcome"),
    "handle_join_welcome": CommandSpec("welcome", PermLevel.MEMBER),
    "handle_leave_notify": CommandSpec("leave", PermLevel.MEMBER),
    "handle_leave_block": CommandSpec("leave"),
    "handle_global_allow": CommandSpec("join"),
    "handle_global_block": CommandSpec("join"),
    # ---------- 群友管理 / 群文件 ----------
    "get_group_member_list": CommandSpec("get_group_member_list", PermLevel.MEMBER),
    "clear_group_member": CommandSpec("clear_group_member", PermLevel.MEMBER),
    "upload_group_file": CommandSpec("upload_group_file", PermLevel.MEMBER, llm_tool="llm_upload_group_file"),
    "delete_group_file": CommandSpec("delete_group_file", llm_tool="llm_delete_group_file"),
    "view_group_file": CommandSpec("view_group_file", PermLevel.MEMBER, llm_tool="llm_view_group_file"),
}

# 反向索引：llm_tool 名 -> 同一 CommandSpec。vote / curfew / join
# 无 llm_tool（command-only，产品决策），误调会 KeyError。
_BY_LLM_TOOL: dict[str, CommandSpec] = {spec.llm_tool: spec for spec in COMMAND_SPECS.values() if spec.llm_tool}


def perm_required_for(method_name: str) -> Callable[..., Callable[..., AsyncGenerator[Any, Any]]]:
    """`perm_required` with arguments from COMMAND_SPECS."""
    spec = COMMAND_SPECS[method_name]
    return perm_required(
        spec.bot_perm,
        perm_key=spec.perm_key,
        check_at=spec.check_at,
        allow_private=spec.allow_private,
    )


def llm_guarded_for(llm_name: str) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., AsyncGenerator[Any, Any]]]:
    """Collapse the `llm_perm_block` preamble and emit-if-result into one gate.

    Stack outside `@filter.llm_tool` so the tool schema still sees the raw
    function::

        @llm_guarded_for("llm_set_group_ban")
        @filter.llm_tool()
        async def llm_set_group_ban(...):
            ...
            return await self.normal.set_group_ban(...)
    """
    spec = _BY_LLM_TOOL[llm_name]

    def decorator(func: Callable[..., Awaitable[Any]]) -> Callable[..., AsyncGenerator[Any, Any]]:
        @wraps(func)
        async def wrapper(plugin_instance: Any, event: Any, *args: Any, **kwargs: Any) -> AsyncGenerator[Any, Any]:
            if error := await perm_manager.llm_perm_block(event, perm_key=spec.perm_key, bot_perm=spec.bot_perm):
                yield error
                return
            if result := await func(plugin_instance, event, *args, **kwargs):
                yield result

        return wrapper

    return decorator
