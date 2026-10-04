# decision.py
"""进群审核的纯判定：一次快照输入，无 IO、无时间源、无副作用。

`decide` 是整个进群模块的测试 seam：给快照 + 申请人 + 时刻，得到
(是否通过, 展示文案, 结果代号, 副作用)。副作用（计数、拉黑、清零）
由调用方（JoinReviewer）执行，本模块只负责说出该做什么。
"""

from collections.abc import Collection
from dataclasses import dataclass, field

# decide 吐出的副作用代号，由 JoinReviewer 按原语义执行。
EFFECT_COUNT_FAIL = "count_fail"  # 失败计数 +1（含 24h 过期语义）
EFFECT_BLOCK = "block"  # 按 use_global_block 拉黑
EFFECT_CLEAR_FAIL = "clear_fail"  # 清零失败计数（封顶拉黑后）


@dataclass(frozen=True)
class GroupJoinSnapshot:
    """判定所需的全部输入，一次加载，不再 N+1 查询。"""

    join_full_reject: bool = True
    join_full_msg: str = "群人数已满"
    is_full: bool = False
    join_single_group: bool = False
    other_group: str | None = None
    earlier_group: str | None = None
    allow_ids: Collection[str] = field(default_factory=list)
    block_ids: Collection[str] = field(default_factory=list)
    join_min_level: int = 8
    join_no_match_msg: bool = False
    join_reject_words: Collection[str] = field(default_factory=list)
    reject_word_block: bool = False
    join_accept_words: Collection[str] = field(default_factory=list)
    join_max_time: int = 3
    fail_count: int = 0
    join_no_match_reject: bool = False


@dataclass(frozen=True)
class Applicant:
    uid: str
    comment: str | None = None
    user_level: int | None = None


@dataclass(frozen=True)
class Decision:
    approve: bool | None
    reason: str
    code: str
    effects: tuple[str, ...] = ()


def decide(snapshot: GroupJoinSnapshot, applicant: Applicant, now: float) -> Decision:
    """纯判定：与 should_approve 原分支顺序、文案、代号完全一致。

    `now` 仅保留给未来过期语义进入判定使用，目前判定不依赖时刻。
    """
    uid, comment, user_level = applicant.uid, applicant.comment, applicant.user_level

    # -1.群满直接拒绝（白名单也不放行）
    if snapshot.join_full_reject and snapshot.is_full:
        return Decision(False, snapshot.join_full_msg or "群人数已满", "full")

    # -0.禁止多群加入
    if snapshot.join_single_group:
        if snapshot.other_group:
            return Decision(False, f"已加入其他群聊({snapshot.other_group})", "multi_group")
        if snapshot.earlier_group:
            return Decision(False, f"已申请其他群聊({snapshot.earlier_group})", "multi_group")

    # 0.白名单直接通过
    if uid in snapshot.allow_ids:
        return Decision(True, "白名单用户", "allow")

    # 1.黑名单
    if uid in snapshot.block_ids:
        return Decision(False, "黑名单用户", "block")

    # 2.QQ等级过低或隐藏
    if user_level is None:
        return Decision(None, "QQ等级可能被隐藏，人工审核", "level_hidden")
    if snapshot.join_min_level > 0 and user_level < snapshot.join_min_level:
        return Decision(False, f"QQ等级过低({user_level}<{snapshot.join_min_level})", "level_low")

    if comment:
        keyword = "\n答案："
        if keyword in comment:
            comment = comment.split(keyword, 1)[1]

    if not comment:
        if snapshot.join_no_match_msg:
            return Decision(False, "验证信息为空", "empty_msg")
    else:
        lower_comment = comment.lower()
        # 3.命中进群黑词
        if any(rk.lower() in lower_comment for rk in snapshot.join_reject_words):
            if snapshot.reject_word_block:
                return Decision(False, "命中进群黑词，已拉黑", "black_word_block", (EFFECT_BLOCK,))
            return Decision(False, "命中进群黑词", "black_word")
        # 4.命中进群白词
        if snapshot.join_accept_words and any(ak.lower() in lower_comment for ak in snapshot.join_accept_words):
            return Decision(True, "命中进群白词", "white_word")

    # 5.最大失败次数（计数副作用由调用方执行，此处只声明）
    count_effect: tuple[str, ...] = ()
    if snapshot.join_max_time > 0:
        if snapshot.fail_count + 1 > snapshot.join_max_time:
            return Decision(
                False,
                f"进群尝试次数已达上限({snapshot.join_max_time}次)，已拉黑",
                "max_fail",
                (EFFECT_BLOCK, EFFECT_CLEAR_FAIL),
            )
        count_effect = (EFFECT_COUNT_FAIL,)

    # 6.未命中白词自动驳回
    if snapshot.join_no_match_reject:
        return Decision(False, "未命中进群关键词", "no_match", count_effect)

    # 7.人工审核
    return Decision(None, "人工审核", "manual", count_effect)
