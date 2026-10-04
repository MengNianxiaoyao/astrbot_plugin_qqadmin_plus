# command_specs.py
"""Single source of truth for command / LLM-tool permission metadata.

`main.py` keeps AstrBot's `@filter.command` / `@filter.llm_tool` literals (the
framework scans those), but every perm-gate argument lives here exactly once.
`perm_required_for` / `llm_guarded_for` are the only readers.

Note: `handle_spamming_ban_time` intentionally uses
`perm_key="handle_builtin_ban_words"`. That is the schema's key for the
刷屏禁言指令 (`_conf_schema.json`), not copy-paste drift. It is pinned here
so the next reader doesn't "fix" it.
"""

from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from functools import wraps
from typing import Any

from .permission import PermLevel, perm_manager, perm_required


@dataclass(frozen=True)
class CommandSpec:
    bot_perm: PermLevel
    perm_key: str
    check_at: bool = True
    allow_private: bool = False


# Keyed by command method name in QQAdminPlugin. Mirrors the @perm_required
# arguments previously scattered across main.py (51 delegating commands;
# set_config / reset_config keep bespoke gates and stay out of this table).
COMMAND_SPECS: dict[str, CommandSpec] = {
    "set_group_ban": CommandSpec(PermLevel.ADMIN, "group_ban"),
    "cancel_group_ban": CommandSpec(PermLevel.ADMIN, "group_ban"),
    "set_group_whole_ban": CommandSpec(PermLevel.ADMIN, "whole_ban"),
    "set_group_card": CommandSpec(PermLevel.ADMIN, "set_group_card"),
    "set_group_special_title": CommandSpec(PermLevel.OWNER, "set_group_special_title"),
    "set_group_special_title_me": CommandSpec(PermLevel.OWNER, "set_group_special_title_me"),
    "set_group_kick": CommandSpec(PermLevel.ADMIN, "set_group_kick"),
    "set_group_block": CommandSpec(PermLevel.ADMIN, "set_group_block"),
    "set_group_admin": CommandSpec(PermLevel.OWNER, "admin", check_at=False),
    "cancel_group_admin": CommandSpec(PermLevel.OWNER, "admin", check_at=False),
    "set_essence_msg": CommandSpec(PermLevel.ADMIN, "essence"),
    "delete_essence_msg": CommandSpec(PermLevel.ADMIN, "essence"),
    "get_essence_msg_list": CommandSpec(PermLevel.ADMIN, "get_essence_msg_list"),
    "set_group_portrait": CommandSpec(PermLevel.ADMIN, "set_group_portrait"),
    "set_group_name": CommandSpec(PermLevel.ADMIN, "set_group_name"),
    "delete_msg": CommandSpec(PermLevel.MEMBER, "delete_msg"),
    "send_group_notice": CommandSpec(PermLevel.ADMIN, "send_group_notice"),
    "get_group_notice": CommandSpec(PermLevel.MEMBER, "get_group_notice"),
    "handle_word_ban_time": CommandSpec(PermLevel.ADMIN, "word_ban"),
    "handle_ban_words": CommandSpec(PermLevel.ADMIN, "word_ban"),
    "handle_builtin_ban_words": CommandSpec(PermLevel.ADMIN, "word_ban"),
    "handle_spamming_ban_time": CommandSpec(PermLevel.ADMIN, "handle_builtin_ban_words"),
    "start_vote_mute": CommandSpec(PermLevel.ADMIN, "vote"),
    "agree_vote_mute": CommandSpec(PermLevel.ADMIN, "vote"),
    "disagree_vote_mute": CommandSpec(PermLevel.ADMIN, "vote"),
    "cancel_vote_mute": CommandSpec(PermLevel.ADMIN, "vote"),
    "start_curfew": CommandSpec(PermLevel.ADMIN, "curfew"),
    "stop_curfew": CommandSpec(PermLevel.ADMIN, "curfew"),
    "handle_join_review": CommandSpec(PermLevel.ADMIN, "join"),
    "handle_accept_words": CommandSpec(PermLevel.ADMIN, "join"),
    "handle_reject_words": CommandSpec(PermLevel.ADMIN, "join"),
    "handle_no_match_reject": CommandSpec(PermLevel.ADMIN, "join"),
    "handle_join_full_reject": CommandSpec(PermLevel.ADMIN, "join"),
    "handle_join_full_msg": CommandSpec(PermLevel.ADMIN, "join"),
    "handle_join_min_level": CommandSpec(PermLevel.ADMIN, "join"),
    "handle_join_max_time": CommandSpec(PermLevel.ADMIN, "join"),
    "handle_allow_ids": CommandSpec(PermLevel.ADMIN, "join"),
    "handle_reject_ids": CommandSpec(PermLevel.ADMIN, "join"),
    "agree_add_group": CommandSpec(PermLevel.ADMIN, "approve", allow_private=True),
    "refuse_add_group": CommandSpec(PermLevel.ADMIN, "approve", allow_private=True),
    "handle_join_ban": CommandSpec(PermLevel.ADMIN, "welcome"),
    "handle_join_welcome": CommandSpec(PermLevel.MEMBER, "welcome"),
    "handle_leave_notify": CommandSpec(PermLevel.MEMBER, "leave"),
    "handle_leave_block": CommandSpec(PermLevel.ADMIN, "leave"),
    "handle_global_allow": CommandSpec(PermLevel.ADMIN, "join"),
    "handle_global_block": CommandSpec(PermLevel.ADMIN, "join"),
    "get_group_member_list": CommandSpec(PermLevel.MEMBER, "get_group_member_list"),
    "clear_group_member": CommandSpec(PermLevel.MEMBER, "clear_group_member"),
    "upload_group_file": CommandSpec(PermLevel.MEMBER, "upload_group_file"),
    "delete_group_file": CommandSpec(PermLevel.ADMIN, "delete_group_file"),
    "view_group_file": CommandSpec(PermLevel.MEMBER, "view_group_file"),
}


@dataclass(frozen=True)
class LlmSpec:
    perm_key: str
    bot_perm: PermLevel = PermLevel.ADMIN


# Keyed by llm_tool method name. Only the 15 existing adapters are covered;
# vote / curfew / join stay command-only (product decision, not this refactor).
LLM_SPECS: dict[str, LlmSpec] = {
    "llm_set_group_ban": LlmSpec("group_ban"),
    "llm_set_group_card": LlmSpec("set_group_card"),
    "llm_set_group_special_title": LlmSpec("set_group_special_title", PermLevel.OWNER),
    "llm_set_group_whole_ban": LlmSpec("whole_ban"),
    "llm_set_group_kick": LlmSpec("set_group_kick"),
    "llm_set_group_block": LlmSpec("set_group_block"),
    "llm_set_essence_msg": LlmSpec("essence"),
    "llm_get_essence_msg_list": LlmSpec("get_essence_msg_list"),
    "llm_set_group_portrait": LlmSpec("set_group_portrait"),
    "llm_set_group_name": LlmSpec("set_group_name"),
    "llm_send_group_notice": LlmSpec("send_group_notice"),
    "llm_get_group_notice": LlmSpec("get_group_notice", PermLevel.MEMBER),
    "llm_upload_group_file": LlmSpec("upload_group_file", PermLevel.MEMBER),
    "llm_delete_group_file": LlmSpec("delete_group_file"),
    "llm_view_group_file": LlmSpec("view_group_file", PermLevel.MEMBER),
}


def perm_required_for(method_name: str) -> Callable[..., Callable[..., AsyncGenerator[Any, Any]]]:
    """`perm_required` with arguments from COMMAND_SPECS."""
    spec = COMMAND_SPECS[method_name]
    return perm_required(
        spec.bot_perm,
        perm_key=spec.perm_key,
        check_at=spec.check_at,
        allow_private=spec.allow_private,
    )


def llm_guarded_for(method_name: str) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., AsyncGenerator[Any, Any]]]:
    """Collapse the `llm_perm_block` preamble and emit-if-result into one gate.

    Stack outside `@filter.llm_tool` so the tool schema still sees the raw
    function::

        @llm_guarded_for("llm_set_group_ban")
        @filter.llm_tool()
        async def llm_set_group_ban(...):
            ...
            return await self.normal.set_group_ban(...)
    """
    spec = LLM_SPECS[method_name]

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
