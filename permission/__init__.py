"""权限体系：超管 > 群主 > 管理员 > 成员，未知身份视为无权限。

- PermLevel：数字越小权限越高；from_str 只认配置合法值，
  非法返回 None，由调用方回退到管理员（宁紧勿松）。
- 判定唯一出口 evaluate_perm（纯函数，可单测）：用户等级 / Bot 等级 /
  被@对象等级三段校验。perm_block 负责取数（用户/Bot/目标等级 +
  按群 perms 取最低等级），装饰器与 LLM 两条路径在此会合。
- @perm_required：群聊命令门面；allow_private 仅超管私聊放行。
  llm_perm_block：LLM 工具门面，额外限制仅群聊 + aiocqhttp。

子模块：levels（等级+纯判定）/ manager（取数+装饰器+单例）/
specs（命令与 LLM 工具权限元数据，全仓唯一出处）。
"""

from .levels import PermLevel, evaluate_perm
from .manager import PermissionManager, perm_manager, perm_required
from .specs import (
    COMMAND_SPECS,
    CommandSpec,
    llm_guarded_for,
    perm_required_for,
)

__all__ = [
    "COMMAND_SPECS",
    "CommandSpec",
    "PermLevel",
    "PermissionManager",
    "evaluate_perm",
    "llm_guarded_for",
    "perm_manager",
    "perm_required",
    "perm_required_for",
]
