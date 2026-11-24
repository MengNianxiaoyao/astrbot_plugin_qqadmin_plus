"""抽奖数据模型与常量。"""

import time
from dataclasses import dataclass, field
from typing import Any

STORE_VERSION = 1
WIZARD_TIMEOUT = 180
MAX_COPIES = 50
MAX_TARGET = 500
MAX_GROUPS = 10
MAX_PARTICIPANTS = 2000
MAX_DURATION = 7 * 86400
MIN_DURATION = 10
MAX_PRIZE_LEN = 50
MAX_ITEM_LEN = 500
MAX_STOCK_TEXT = 8000
MAX_HISTORY = 20
MAX_TITLES = 10
SAVE_DEBOUNCE = 1.0


@dataclass
class Lottery:
    group_id: str
    groups: list[str]
    prize: str
    copies: int
    mode: str
    starter: str
    label: str
    delivery: str = "manual"
    stock: list[str] = field(default_factory=list)
    delivery_failures: list[tuple[str, str]] = field(default_factory=list)
    participants: dict[str, str] = field(default_factory=dict)
    winners: list[tuple[str, str]] = field(default_factory=list)
    min_level: int = 0
    titles: list[str] = field(default_factory=list)
    expire_at: float | None = None
    target_count: int | None = None
    max_participants: int | None = None
    deadline_label: str = ""
    created_at: float = field(default_factory=time.time)
    drawn_at: float | None = None
    closed: bool = False
    closing: bool = False
    bot_id: str = ""
    client: Any = None


def instant_chance(lottery: Lottery) -> float:
    """即抽即开单次参与的中奖概率：奖品份数 / 参与人数上限。"""
    if not lottery.max_participants:
        return 0.0
    return min(1.0, lottery.copies / lottery.max_participants)


def finished_reason(lottery: Lottery, now: float | None = None) -> str | None:
    """开奖结束条件的唯一出口：返回结束原因，未结束返回 None。"""
    now = time.time() if now is None else now
    if lottery.mode == "instant":
        if len(lottery.winners) >= lottery.copies:
            return "名额已抽完"
        if lottery.max_participants and len(lottery.participants) >= lottery.max_participants:
            return "参与人数已达上限"
        if lottery.expire_at and lottery.expire_at <= now:
            return "已到截止时间"
        return None
    if lottery.mode == "count":
        if lottery.target_count and len(lottery.participants) >= lottery.target_count:
            return lottery.label or "达到开奖人数"
        return None
    if lottery.mode == "time" and lottery.expire_at and lottery.expire_at <= now:
        return "已到开奖时间"
    return None
