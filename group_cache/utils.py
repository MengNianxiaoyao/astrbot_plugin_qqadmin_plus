"""群信息纯函数：OneBot 响应归一化/群摘要/排序/错误格式化，无 IO、无状态。

缓存类（.cache.QQGroupInfoCache）只管抓取、并发、TTL 与失效计票；
这里只做可单测的数据变换。新人先读本文件再看缓存管线。
"""

from typing import Any

BOT_ROLE_PRIORITY = {
    "owner": 0,
    "admin": 1,
    "member": 2,
    "unknown": 2,
}


def format_error(exc: BaseException) -> str:
    """异常类型 + 信息双保险：str(exc) 为空时回退到 repr，避免日志只剩一个冒号。"""
    message = str(exc).strip() or repr(exc)
    return f"{type(exc).__name__}: {message}"


def extract_list(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    return []


def extract_object(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, dict):
            return data
        return result
    return {}


def normalize_group_summary(raw_group: dict[str, Any]) -> dict[str, Any]:
    group_id = str(raw_group.get("group_id", "")).strip()
    return {
        "group_id": group_id,
        "group_name": str(raw_group.get("group_name", "")).strip() or f"群 {group_id}",
        "avatar": build_avatar(group_id),
        "member_count": safe_int(raw_group.get("member_count"), 0),
        "max_member_count": safe_int(raw_group.get("max_member_count"), 0),
        "source": "live",
    }


def needs_detail_refresh(raw_group: dict[str, Any], group_id: str) -> bool:
    # 真实群至少有机器人在内，人数全 0 即视为缺数，一并补详情自愈
    missing_counts = not safe_int(raw_group.get("member_count"), 0) and not safe_int(raw_group.get("max_member_count"), 0)
    return not str(raw_group.get("group_name", "")).strip() or not group_id or missing_counts


def build_fallback_group(group_id: str) -> dict[str, Any]:
    return {
        "group_id": group_id,
        "group_name": f"群 {group_id}",
        "avatar": build_avatar(group_id),
        "member_count": 0,
        "max_member_count": 0,
        "source": "cached",
    }


def build_avatar(group_id: str) -> str:
    # 列表缩略图仅 38px 展示（2x 屏也足够），640 是十几倍浪费
    return f"https://p.qlogo.cn/gh/{group_id}/{group_id}/100"


def safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_bot_role(value: Any) -> str:
    role = str(value or "").strip().lower()
    if role in {"owner", "admin", "member"}:
        return role
    return "unknown"


def sort_groups(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        groups,
        key=lambda item: (
            BOT_ROLE_PRIORITY.get(str(item.get("bot_role", "unknown")), 2),
            not str(item.get("group_id", "")).isdigit(),
            int(item["group_id"]) if str(item.get("group_id", "")).isdigit() else 0,
            item.get("group_name", ""),
        ),
    )
