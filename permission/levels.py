"""权限等级与纯判定：无 IO、无配置读取，可单测。

- PermLevel：数字越小权限越高；from_str 只认配置合法值，
  非法返回 None，由调用方回退到管理员（宁紧勿松）。
- evaluate_perm：判定唯一出口，给定各方等级返回阻断文案（None 表示放行）。
"""

from collections.abc import Collection
from enum import IntEnum


class PermLevel(IntEnum):
    """
    定义用户的权限等级。数字越小，权限越高。
    """

    SUPERUSER = 0
    OWNER = 1
    ADMIN = 2
    MEMBER = 4
    UNKNOWN = 5

    def __str__(self):
        return {
            PermLevel.SUPERUSER: "超管",
            PermLevel.OWNER: "群主",
            PermLevel.ADMIN: "管理员",
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
