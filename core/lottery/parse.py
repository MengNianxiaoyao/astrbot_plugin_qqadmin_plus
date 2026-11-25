"""抽奖输入解析：开奖方式、时间、份数、门槛、发放方式与预存奖品。"""

import re
import zoneinfo
from datetime import datetime, timedelta

from ...utils import format_time, parse_clock
from .models import (
    MAX_COPIES,
    MAX_DURATION,
    MAX_ITEM_LEN,
    MAX_PARTICIPANTS,
    MAX_PRIZE_LEN,
    MAX_STOCK_TEXT,
    MAX_TARGET,
    MAX_TITLES,
    MIN_DURATION,
)

_UNIT_SECONDS = {"秒": 1, "分钟": 60, "分": 60, "小时": 3600, "时": 3600, "天": 86400}
_DURATION_RE = re.compile(r"^(\d+)\s*(秒|分钟|分|小时|时|天)$")
_MONTH_DAY_RE = re.compile(r"(\d{1,2})[-/](\d{1,2})\s+(\d{1,2})[:：](\d{1,2})")
_LEVEL_RE = re.compile(r"^(?:等级|群等级)(\d+)$")
_LEVEL_RE_ALT = re.compile(r"^(\d+)级$")
_TITLE_RE = re.compile(r"^(?:头衔|群头衔)(.+)$")
_TITLE_SPLIT_RE = re.compile(r"[、,，/|]+")

MODE_PROMPT = (
    "请选择开奖方式（直接发送其中一种）：\n"
    "· 时间12-25 20:30 / 时间10分钟 —— 按时间开奖（月日时分需晚于当前时间）\n"
    "· 人数20 —— 满 20 人开奖\n"
    "· 即开 —— 即抽即开，每次参与立即判定"
)
COPIES_PROMPT = "请输入奖品份数（1~50，默认 1 份，直接发送数字或“默认”）"
CAP_PROMPT = (
    "请输入参与人数上限（不少于奖品份数 {copies}，最多 {max_cap}）。\n即抽即开的中奖概率 = 奖品份数 / 参与人数上限。"
)
DEADLINE_PROMPT = (
    "请输入参与截止时间（例如 12-25 20:30、10分钟、1小时）。\n"
    "月日时分需晚于当前时间；倒计时最短 10 秒。到截止时间无论是否抽完都会结束，剩余奖品不再发放。"
)
DELIVERY_PROMPT = (
    "奖品发放方式：\n"
    "· 回复 1 = 人工发放（中奖者联系发起人领取）\n"
    "· 回复 2 = 预存奖品（提前存入机器人，开奖后中奖者私聊机器人发送「/领奖」领取）"
)
STOCK_PROMPT = (
    "请发送 {copies} 份奖品内容，每行一份（也可用 | 分隔）。\n"
    "内容只在领奖时私聊返回给中奖者，不会在群里显示。发送「取消」退出。"
)
THRESHOLD_PROMPT = (
    "请设置参与门槛（可组合，例如：等级10 头衔欧皇 头衔管理）；\n"
    "只写等级时按等级拦截，只写头衔时按头衔放行；两者都写时默认满足任意一项即可参与，\n"
    "写成「等级10 且 头衔管理」则要求同时满足等级和头衔。多个头衔满足其一，也可写成 头衔欧皇、管理。\n"
    "不需要门槛请发送“无”。门槛只对本次抽奖生效。"
)
TIME_ERROR_PROMPT = f"无法识别时间。\n{DEADLINE_PROMPT}"


def clock_label(timestamp: float, tz: zoneinfo.ZoneInfo, suffix: str) -> str:
    """把时间戳格式化为 `MM-DD HH:MM 后缀`，供问答提示与开奖公告共用。"""
    return f"{format_time(timestamp, '%m-%d %H:%M', tz)} {suffix}"


def _parse_month_day(target: str, tz: zoneinfo.ZoneInfo, suffix: str) -> tuple[float | None, None, str] | None:
    """解析 `12-25 20:30` / `12月25日20:30`；只接今年内、晚于当前时间的时刻。"""
    normalized = target.replace("月", "-").replace("日", " ").strip()
    match = _MONTH_DAY_RE.fullmatch(normalized)
    if not match:
        return None
    month, day, hour, minute = (int(part) for part in match.groups())
    now = datetime.now(tz)
    try:
        at = datetime(now.year, month, day, hour, minute, tzinfo=tz)
    except ValueError:
        return None
    if at <= now:
        # 月份已过（如 1 月站在 12 月设置 1 月）：按明年的同一日期计算
        if month < now.month:
            try:
                at = datetime(now.year + 1, month, day, hour, minute, tzinfo=tz)
            except ValueError:
                return None
        else:
            return None
    return at.timestamp(), None, clock_label(at.timestamp(), tz, suffix)


def parse_deadline(
    value: str, tz: zoneinfo.ZoneInfo, suffix: str = "截止"
) -> tuple[float | None, int | None, str] | None:
    """解析截止时间，返回 (expire_at, 倒计时秒数, 展示文案)；非法返回 None。

    支持 `12-25 20:30` / `12月25日20:30` 的月日时分、`10分钟` / `1小时` 的倒计时，
    以及 `20:30` 这类时刻（已过则顺延到次日）；倒计时由调用方在创建时起算。
    """
    target = value.strip()
    duration = _DURATION_RE.fullmatch(target)
    if duration:
        seconds = int(duration.group(1)) * _UNIT_SECONDS[duration.group(2)]
        if not MIN_DURATION <= seconds <= MAX_DURATION:
            return None
        return None, seconds, f"{target}后{suffix}"

    scheduled = _parse_month_day(target, tz, suffix)
    if scheduled is not None:
        return scheduled

    clock = parse_clock(target)
    if clock:
        hour, minute = clock
        now = datetime.now(tz)
        at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if at <= now:
            at += timedelta(days=1)
        return at.timestamp(), None, clock_label(at.timestamp(), tz, suffix)
    return None


def parse_draw_mode(value: str, tz: zoneinfo.ZoneInfo) -> tuple[str, float | None, int | None, str, int | None] | None:
    """解析开奖方式，返回 (mode, expire_at, target_count, 展示文案, 倒计时秒数)；非法返回 None。"""
    text = value.strip()
    if text in {"即开", "即抽即开", "即时"}:
        return "instant", None, None, "即抽即开（每次参与立即判定）", None

    match = re.fullmatch(r"(?:时间|定时)\s*(.+)", text)
    if match:
        parsed = parse_deadline(match.group(1), tz, "开奖")
        if parsed is None:
            return None
        expire_at, duration, label = parsed
        return "time", expire_at, None, label, duration

    match = re.fullmatch(r"(?:人数|满)\s*(\d+)\s*人?", text) or re.fullmatch(r"(\d+)\s*人", text)
    if match:
        target = int(match.group(1))
        if not 1 <= target <= MAX_TARGET:
            return None
        return "count", None, target, f"满 {target} 人开奖", None
    return None


_THRESHOLD_ALL_TOKENS = {"且", "并", "同时", "and", "AND", "And"}


def parse_threshold(value: str) -> tuple[int, list[str], bool] | None:
    """解析参与门槛，返回 (最低群等级, 头衔列表, 是否两者都要满足)，非法返回 None。

    默认等级与头衔满足其一即可；写成 `等级10 且 头衔管理` 时两者都必须满足。
    """
    text = value.strip()
    if text in {"无", "没有", "跳过", "不设", "默认"}:
        return 0, [], False
    min_level, titles = 0, []
    require_all = False
    for token in text.split():
        if token in _THRESHOLD_ALL_TOKENS:
            require_all = True
            continue
        match = _LEVEL_RE.fullmatch(token) or _LEVEL_RE_ALT.fullmatch(token)
        if match:
            min_level = int(match.group(1))
            continue
        match = _TITLE_RE.fullmatch(token)
        if match:
            for item in _TITLE_SPLIT_RE.split(match.group(1)):
                title = item.strip()
                if not title:
                    continue
                if len(title) > 20:
                    return None
                if title not in titles:
                    titles.append(title)
            continue
        return None
    if not min_level and not titles:
        return None
    if not 0 <= min_level <= 100 or len(titles) > MAX_TITLES:
        return None
    # 只有一个条件时“且/或”没有区别，统一按任一处理
    return min_level, titles, require_all and bool(min_level and titles)


def parse_delivery(value: str) -> str | None:
    text = value.strip().lower()
    if text in {"1", "人工", "手动", "manual"}:
        return "manual"
    if text in {"2", "预存", "私聊", "private"}:
        return "private"
    return None


def parse_stock(value: str, copies: int) -> tuple[list[str], str | None]:
    """按行或 | 切分预存奖品，份数必须与奖品份数一致。"""
    if len(value) > MAX_STOCK_TEXT:
        return [], f"奖品内容过长（上限 {MAX_STOCK_TEXT} 字），请减少后重新发送"
    items = [line.strip() for line in value.splitlines() if line.strip()]
    # 单行时兼容用 | 分隔；多行内容里的 | 视为奖品正文，避免把卡密拆坏
    if len(items) != copies and "\n" not in value and "|" in value:
        items = [part.strip() for part in value.split("|") if part.strip()]
    if len(items) != copies:
        return [], f"需要 {copies} 份奖品，当前收到 {len(items)} 份，请重新发送（每行一份或用 | 分隔）"
    if any(len(item) > MAX_ITEM_LEN for item in items):
        return [], f"单份奖品内容不能超过 {MAX_ITEM_LEN} 字，请重新发送"
    return items, None


def validate_prize(text: str) -> str | None:
    if not text or len(text) > MAX_PRIZE_LEN:
        return f"奖品名称需在 {MAX_PRIZE_LEN} 字以内，请重新输入"
    return None


def validate_copies(text: str) -> int | None:
    copies = 1 if text in {"默认", "无"} else (int(text) if text.isdigit() else None)
    return copies if copies is not None and 1 <= copies <= MAX_COPIES else None


def validate_cap(text: str, copies: int) -> int | None:
    cap = int(text) if text.isdigit() else None
    return cap if cap is not None and copies <= cap <= MAX_PARTICIPANTS else None
