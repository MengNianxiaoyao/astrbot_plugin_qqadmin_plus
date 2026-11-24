"""抽奖文案渲染：活动公告、结果、往期与进行中查询（纯函数，便于单测）。"""

import time
import zoneinfo

from ...utils import format_time
from .models import Lottery, instant_chance, participant_total


def threshold_text(lottery: Lottery) -> str:
    parts = []
    if lottery.min_level:
        parts.append(f"群等级≥{lottery.min_level}")
    if lottery.titles:
        parts.append("头衔" + "/".join(f"「{title}」" for title in lottery.titles))
    return "、".join(parts) or "无"


def when_text(timestamp: float | None, tz: zoneinfo.ZoneInfo, fmt: str = "%m-%d %H:%M") -> str:
    if not timestamp:
        return "未知时间"
    return format_time(timestamp, fmt, tz)


def time_left_text(lottery: Lottery, suffix: str = "开奖") -> str:
    left = int(max(0, (lottery.expire_at or 0) - time.time()))
    if left >= 3600:
        hours, remain = divmod(left, 3600)
        return f"约 {hours} 小时 {remain // 60} 分钟后{suffix}"
    if left >= 60:
        return f"约 {left // 60} 分钟后{suffix}"
    return f"约 {left} 秒后{suffix}"


def join_progress_text(lottery: Lottery) -> str:
    if lottery.mode == "count":
        return f"已参与（{participant_total(lottery)}/{lottery.target_count} 人）"
    return f"已参与（当前 {participant_total(lottery)} 人，{time_left_text(lottery)}）"


def summary_lines(lottery: Lottery) -> list[str]:
    """开始公告与进行中详情共用的基础字段。"""
    lines = [
        f"奖品：{lottery.prize}（{lottery.copies} 份）",
        f"开奖方式：{lottery.label}",
        f"参与门槛：{threshold_text(lottery)}",
    ]
    if len(lottery.groups) > 1:
        lines.append(f"参与群：{'、'.join(lottery.groups)}")
    lines.append("奖品发放：" + ("开奖后机器人私聊发送" if lottery.delivery == "private" else "中奖者联系发起人领取"))
    return lines


def active_text(lottery: Lottery) -> str:
    lines = ["【进行中的抽奖】", *summary_lines(lottery)]
    if lottery.mode == "time" and lottery.expire_at:
        lines.append(f"剩余时间：{time_left_text(lottery)}")
        lines.append(f"参与人数：{participant_total(lottery)}")
    elif lottery.mode == "count":
        lines.append(f"参与人数：{participant_total(lottery)}/{lottery.target_count}")
    else:
        cap = lottery.max_participants or 0
        lines.append(f"参与人数：{participant_total(lottery)}/{cap}")
        lines.append(f"已中奖：{len(lottery.winners)}/{lottery.copies}")
        lines.append(f"中奖概率：{instant_chance(lottery):.2%}")
        if lottery.expire_at:
            lines.append(f"剩余时间：{time_left_text(lottery, '截止')}")
    lines.append("发送「/参与抽奖」参加")
    return "\n".join(lines)


def result_lines(
    lottery: Lottery,
    tz: zoneinfo.ZoneInfo,
    title: str,
    reason: str = "",
    show_drawn_at: bool = True,
    empty_text: str = "无人中奖",
    remaining_hint: bool = False,
) -> list[str]:
    """开奖公告与查询结果共用同一份名单/发放说明，避免两处文案漂移。"""
    head = f"{title}（{reason}）" if reason else title
    lines = [head, f"奖品：{lottery.prize}"]
    if len(lottery.groups) > 1:
        lines.append(f"参与群：{'、'.join(lottery.groups)}")
    if show_drawn_at and lottery.drawn_at:
        lines.append(f"开奖时间：{when_text(lottery.drawn_at, tz, '%Y-%m-%d %H:%M')}")
    if lottery.mode == "instant" and lottery.max_participants:
        lines.append(f"参与人数：{participant_total(lottery)}/{lottery.max_participants}")
    else:
        lines.append(f"参与人数：{participant_total(lottery)}")
    if lottery.winners:
        lines.append("中奖名单：")
        for idx, (uid, name) in enumerate(lottery.winners, 1):
            group_id = lottery.user_groups.get(uid)
            suffix = f"｜群 {group_id}" if group_id else ""
            lines.append(f"{idx}. {name}（{uid}）{suffix}")
        if lottery.delivery == "private":
            if lottery.delivery_failures:
                names = "、".join(f"{name}（{uid}）" for uid, name in lottery.delivery_failures)
                lines.append(f"奖品已私聊发送；发送失败已转交发起人：{names}")
            else:
                lines.append("奖品已私聊发送，请中奖者注意查收")
        else:
            lines.append("请中奖者联系发起人领取奖品")
    else:
        lines.append(empty_text)
    if remaining_hint and lottery.mode == "instant" and len(lottery.winners) < lottery.copies:
        lines.append(f"剩余 {lottery.copies - len(lottery.winners)} 份奖品未发放")
    return lines


def history_text(history: list[Lottery], tz: zoneinfo.ZoneInfo) -> str:
    if not history:
        return "本群暂无往期抽奖记录"
    lines = [f"【往期抽奖】最近 {len(history)} 场（发送「/抽奖查询 往期 序号」查看详情）"]
    for idx, item in enumerate(history, 1):
        lines.append(
            f"{idx}. {item.prize}（{item.copies} 份）｜{when_text(item.drawn_at, tz)}｜中奖 {len(item.winners)} 人"
        )
    return "\n".join(lines)


def user_history_text(history: list[Lottery], user_id: str, tz: zoneinfo.ZoneInfo) -> str:
    wins = [
        (idx, item)
        for idx, item in enumerate(history, 1)
        if any(winner_id == user_id for winner_id, _name in item.winners)
    ]
    if not wins:
        return f"用户 {user_id} 暂无往期中奖记录"
    lines = [f"【往期抽奖】用户 {user_id} 共中奖 {len(wins)} 次"]
    for idx, item in wins:
        lines.append(f"{idx}. {item.prize}｜{when_text(item.drawn_at, tz)}")
    return "\n".join(lines)
