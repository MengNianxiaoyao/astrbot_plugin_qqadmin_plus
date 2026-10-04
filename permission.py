import inspect
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Collection
from enum import IntEnum
from functools import wraps
from typing import Any, cast

from astrbot import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .config import PluginConfig
from .data import QQAdminDB
from .utils import get_ats


class PermLevel(IntEnum):
    """
    定义用户的权限等级。数字越小，权限越高。
    """

    SUPERUSER = 0
    OWNER = 1
    ADMIN = 2
    HIGH = 3
    MEMBER = 4
    UNKNOWN = 5

    def __str__(self):
        return {
            PermLevel.SUPERUSER: "超管",
            PermLevel.OWNER: "群主",
            PermLevel.ADMIN: "管理员",
            PermLevel.HIGH: "高等级成员",
            PermLevel.MEMBER: "成员",
            PermLevel.UNKNOWN: "未知/无权限",
        }.get(self, "未知/无权限")

    @classmethod
    def from_str(cls, perm_str: str):
        """
        将权限字符串解析为权限等级。
        仅能识别配置中的合法取值；无法识别时返回 None，由调用方决定安全的回退策略，
        避免把恶意/无效配置解析为最低权限等级（UNKNOWN）从而放行所有用户。
        """
        mapping = {
            "超管": cls.SUPERUSER,
            "群主": cls.OWNER,
            "管理员": cls.ADMIN,
            "高等级成员": cls.HIGH,
            "成员": cls.MEMBER,
        }
        return mapping.get(str(perm_str or "").strip())


def evaluate_perm(
    user_level: PermLevel,
    bot_level: PermLevel,
    required_level: PermLevel,
    bot_required: PermLevel,
    target_levels: Collection[PermLevel] = (),
) -> str | None:
    """纯判定：给定各方等级，返回阻断文案（None 表示放行）。

    权限模块的测试 seam：不触网络、不读配置；`perm_block` 只负责取数，
    判定只走这里。装饰器与 LLM 两条路径早已在 `perm_block` 会合。
    """
    if user_level > required_level:
        return f"你没{required_level}权限"
    if bot_level > bot_required:
        return f"我没{bot_required}权限"
    for target_level in target_levels:
        if bot_level >= target_level:
            return f"我动不了{target_level}"
    return None


class PermissionManager:
    _initialized = False

    def __init__(self):
        self.cfg: PluginConfig | None = None
        self.db: QQAdminDB | None = None
        self._perm_cache: dict[tuple[str, str], tuple[PermLevel, float]] = {}
        self._perm_ttl = 10.0

    def lazy_init(self, config: PluginConfig, db: QQAdminDB):
        if self._initialized:
            logger.warning("PermissionManager already initialized, refreshing instead")
            self.refresh(config, db)
            return
        self.cfg = config
        self.db = db
        self._initialized = True

    def refresh(self, config: PluginConfig, db: QQAdminDB | None = None):
        self.cfg = config
        if db is not None:
            self.db = db
        self._initialized = True

    async def get_perm_level(self, event: AiocqhttpMessageEvent, user_id: str | int) -> PermLevel:
        group_id = event.get_group_id()
        try:
            if not str(group_id).isdigit() or not str(user_id).isdigit() or int(group_id) == 0 or int(user_id) == 0:
                return PermLevel.UNKNOWN
        except Exception:
            return PermLevel.UNKNOWN
        if self.cfg and str(user_id) in self.cfg.admins_id:
            return PermLevel.SUPERUSER
        cache_key = (str(group_id), str(user_id))
        cached = self._perm_cache.get(cache_key)
        if cached and time.time() - cached[1] < self._perm_ttl:
            return cached[0]
        try:
            info = await event.bot.get_group_member_info(group_id=int(group_id), user_id=int(user_id), no_cache=True)
        except Exception:
            return PermLevel.UNKNOWN
        role = info.get("role", "unknown")
        level = int(info.get("level", 0))
        group_config = self.db.get_group_snapshot(group_id) if self.db is not None else {"level_threshold": self.cfg.level_threshold if self.cfg else 50}
        level_threshold = int(group_config.get("level_threshold", 50))
        match role:
            case "owner":
                lvl = PermLevel.OWNER
            case "admin":
                lvl = PermLevel.ADMIN
            case "member":
                lvl = PermLevel.HIGH if level >= level_threshold else PermLevel.MEMBER
            case _:
                lvl = PermLevel.UNKNOWN
        self._perm_cache[cache_key] = (lvl, time.time())
        # 简单容量控制
        if len(self._perm_cache) > 500:
            oldest = min(self._perm_cache.items(), key=lambda kv: kv[1][1])[0]
            self._perm_cache.pop(oldest, None)
        return lvl

    async def perm_block(
        self,
        event: AiocqhttpMessageEvent,
        bot_perm: PermLevel,
        perm_key: str,
        check_at: bool = True,
    ) -> str | None:
        user_level = await self.get_perm_level(event, user_id=event.get_sender_id())

        # 未指定权限，则默认至少需要管理员权限
        group_config = self.db.get_group_snapshot(event.get_group_id()) if self.db is not None else {"perms": self.cfg.perms if self.cfg else {}}
        perms = group_config.get("perms", {})
        required_level = PermLevel.from_str(str(perms.get(perm_key, "管理员")))
        if required_level is None:
            # 配置中权限值为无效项时回退到管理员，防止权限放松导致越权
            required_level = PermLevel.ADMIN

        bot_level = await self.get_perm_level(event, user_id=event.get_self_id())

        target_levels = []
        if check_at:
            for at_id in get_ats(event):
                target_levels.append(await self.get_perm_level(event, user_id=at_id))

        return evaluate_perm(user_level, bot_level, required_level, bot_perm, target_levels)

    async def llm_perm_block(
        self,
        event: AiocqhttpMessageEvent,
        perm_key: str,
        bot_perm: PermLevel = PermLevel.ADMIN,
    ) -> str | None:
        if event.platform_meta.name != "aiocqhttp":
            return "该工具仅支持通过 QQ 群聊调用"

        if event.is_private_chat():
            return "该工具仅支持在 QQ 群聊中调用"

        if not self._initialized:
            logger.error(
                "PermissionManager is not initialized while checking LLM tool permission: %s",
                perm_key,
            )
            return "内部错误：权限系统尚未正确加载"

        return await self.perm_block(
            event,
            bot_perm=bot_perm,
            perm_key=perm_key,
            check_at=False,
        )


perm_manager = PermissionManager()


def perm_required(
    bot_perm: PermLevel = PermLevel.ADMIN,
    perm_key: str | None = None,
    check_at: bool = True,
    allow_private: bool = False,
):
    """
    权限检查装饰器。
    :param perm_key: 可选。用户执行命令所需的最低权限键名，默认使用被装饰函数的函数名。
    :param bot_perm: Bot 执行此命令所需的最低权限等级。
    :param check_at: 是否检查“是否有权对被@者实施操作”。
    :param allow_private: 超管是否可在私聊中使用该命令；默认 False，私聊一律拒绝。
    """

    def decorator(
        func: Callable[..., AsyncGenerator[Any, Any] | Awaitable[Any]],
    ) -> Callable[..., AsyncGenerator[Any, Any]]:
        actual_perm_key = perm_key or func.__name__

        @wraps(func)
        async def wrapper(
            plugin_instance: Any,
            event: AiocqhttpMessageEvent,
            *args: Any,
            **kwargs: Any,
        ) -> AsyncGenerator[Any, Any]:

            # 仅限aiocqhttp
            if event.platform_meta.name != "aiocqhttp":
                return

            # 权限管理未初始化（私聊/群聊共用一次判断）
            if not perm_manager._initialized or perm_manager.cfg is None:
                logger.error(f"PermissionManager 未初始化（尝试访问权限项：{actual_perm_key}）")
                yield event.plain_result("内部错误：权限系统未正确加载")
                event.stop_event()
                return

            # 私聊处理：超管 + 命令显式放行才可执行，其余拒绝
            if event.is_private_chat():
                if str(event.get_sender_id()) not in (perm_manager.cfg.admins_id or []):
                    yield event.plain_result("该命令仅支持在群聊中使用")
                    event.stop_event()
                    return
                if not allow_private:
                    yield event.plain_result("该命令不支持私聊使用，请在群聊中使用")
                    event.stop_event()
                    return
                if inspect.isasyncgenfunction(func):
                    async for item in func(plugin_instance, event, *args, **kwargs):
                        yield item
                else:
                    await cast(Awaitable[Any], func(plugin_instance, event, *args, **kwargs))
                return

            # 判断权限
            result = await perm_manager.perm_block(event, bot_perm=bot_perm, perm_key=actual_perm_key, check_at=check_at)
            if result:
                yield event.plain_result(result)
                event.stop_event()
                return

            # 执行原始方法
            if inspect.isasyncgenfunction(func):
                async for item in func(plugin_instance, event, *args, **kwargs):
                    yield item
            else:
                await cast(Awaitable[Any], func(plugin_instance, event, *args, **kwargs))

        return wrapper

    return decorator
