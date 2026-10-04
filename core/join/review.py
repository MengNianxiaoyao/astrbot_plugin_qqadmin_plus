import asyncio
import time

from astrbot.api import logger

from ...data import QQAdminDB, QQAdminGlobalList
from ...utils import resolve_allow_ids, resolve_block_ids
from .decision import (
    EFFECT_BLOCK,
    EFFECT_CLEAR_FAIL,
    EFFECT_COUNT_FAIL,
    Applicant,
    GroupJoinSnapshot,
    decide,
)
from .state import JoinState


class JoinReviewer:
    """进群审核判定：白/黑名单、群满、多群、等级、关键词、次数上限。"""

    def __init__(
        self,
        db: QQAdminDB,
        global_list: QQAdminGlobalList,
        group_cache,
        state: JoinState,
    ):
        self.db = db
        self.global_list = global_list
        self._group_cache = group_cache
        self.state = state

    async def _is_group_full(self, gid: str, client=None) -> bool:
        """群人数是否已满。无法获取到有效人数时返回 False（放行，走正常审核流程）。"""

        def _counts(info: dict | None) -> tuple[int, int] | None:
            if not isinstance(info, dict):
                return None
            # 兼容 call_action 包裹的 {"data": {...}} 结构
            data = info.get("data")
            if isinstance(data, dict):
                info = data
            try:
                member_count = int(info.get("member_count") or 0)
                max_count = int(info.get("max_member_count") or 0)
            except (TypeError, ValueError):
                return None
            if member_count <= 0 or max_count <= 0:
                return None
            return member_count, max_count

        # 1. 优先用群缓存（强制刷新拿到最新人数）
        if self._group_cache:
            try:
                group = await self._group_cache.get_group(gid, force=True)
                counts = _counts(group)
                if counts:
                    member_count, max_count = counts
                    return member_count >= max_count
            except Exception:
                pass

        # 2. 兜底直查 OneBot 接口
        if client is not None:
            try:
                info = None
                if hasattr(client, "get_group_info"):
                    try:
                        info = await client.get_group_info(group_id=int(gid))
                    except TypeError:
                        info = await client.get_group_info(group_id=int(gid), no_cache=True)
                elif hasattr(client, "call_action"):
                    info = await client.call_action("get_group_info", group_id=int(gid))
                counts = _counts(info if isinstance(info, dict) else None)
                if counts:
                    member_count, max_count = counts
                    return member_count >= max_count
            except Exception as e:
                logger.debug(f"获取群 {gid} 人数失败，跳过满群检查: {e}")

        return False

    async def _find_other_group(self, gid: str, uid: str, client=None) -> str | None:
        """检查用户是否已在同账号管理的其他群中，返回群名，否返回 None。"""
        if client is None or not self._group_cache:
            return None
        try:
            groups = await self._group_cache.list_groups()
        except Exception as e:
            logger.debug(f"获取群列表失败，跳过多群检查: {e}")
            return None
        others = [
            (str(g.get("group_id", "")), g.get("group_name", "") or str(g.get("group_id", "")))
            for g in groups
            if isinstance(g, dict) and str(g.get("group_id", "")) and str(g.get("group_id", "")) != gid
        ]
        if not others:
            return None

        async def _check(item: tuple[str, str]) -> str | None:
            ngid, name = item
            try:
                try:
                    # 单次探测超时封顶：部分 OneBot 实现在查询非成员时可能无响应
                    info = await asyncio.wait_for(
                        client.get_group_member_info(group_id=int(ngid), user_id=int(uid), no_cache=True),
                        timeout=10,
                    )
                except TypeError:
                    info = await asyncio.wait_for(
                        client.get_group_member_info(group_id=int(ngid), user_id=int(uid)),
                        timeout=10,
                    )
            except Exception:
                # 探测失败按“不在该群”处理，不阻断正常审核
                return None
            return name if info else None

        for name in await asyncio.gather(*[_check(item) for item in others]):
            if name:
                return name
        return None

    async def add_to_block(self, gid: str, uid: str):
        """向群黑名单或全局黑名单添加用户（根据 use_global_block 判断）"""
        if await self.db.get(gid, "use_global_block", False):
            self.global_list.add("block", uid)
        else:
            await self.db.add(gid, "block_ids", uid)

    async def _load_snapshot(self, gid: str, uid: str, client=None) -> GroupJoinSnapshot:
        """一次加载判定所需的全部输入（含条件 IO：开关关闭时不触发网络探测）。"""
        full_reject = await self.db.get(gid, "join_full_reject", True)
        single_group = await self.db.get(gid, "join_single_group", False)
        other = await self._find_other_group(gid, uid, client) if single_group else None
        earlier = self.state.find_earlier_application(uid, gid) if single_group and not other else None
        max_fail = await self.db.get(gid, "join_max_time", 3)
        return GroupJoinSnapshot(
            join_full_reject=full_reject,
            join_full_msg=await self.db.get(gid, "join_full_msg", "群人数已满"),
            is_full=await self._is_group_full(gid, client) if full_reject else False,
            join_single_group=single_group,
            other_group=other,
            earlier_group=earlier,
            allow_ids=await resolve_allow_ids(self.db, self.global_list, gid),
            block_ids=await resolve_block_ids(self.db, self.global_list, gid),
            join_min_level=await self.db.get(gid, "join_min_level", 8),
            join_no_match_msg=await self.db.get(gid, "join_no_match_msg", False),
            join_reject_words=await self.db.get(gid, "join_reject_words", []),
            reject_word_block=await self.db.get(gid, "reject_word_block", False),
            join_accept_words=await self.db.get(gid, "join_accept_words", []),
            join_max_time=max_fail,
            fail_count=self.state.fail_count(f"{gid}_{uid}") if max_fail > 0 else 0,
            join_no_match_reject=await self.db.get(gid, "join_no_match_reject"),
        )

    async def should_approve(
        self,
        gid: str,
        uid: str,
        comment: str | None = None,
        user_level: int | None = None,
        client=None,
    ) -> tuple[bool | None, str, str]:
        """判断是否让该用户入群，返回(是否通过, 展示文案, 结果代号)。

        结果代号稳定不变，可用于免通知等逻辑判断；
        展示文案可自定义或含动态内容，仅用于展示，不得用于逻辑判断。
        代号：full群满 / allow白名单 / block黑名单 / multi_group多群加入
        / level_hidden等级隐藏 / level_low等级过低 / empty_msg验证为空 / black_word命中黑词
        / black_word_block命中黑词已拉黑 / white_word命中白词
        / max_fail超次已拉黑 / no_match未命中驳回 / manual人工审核
        """
        snapshot = await self._load_snapshot(gid, uid, client)
        decision = decide(snapshot, Applicant(uid, comment, user_level), time.time())
        key = f"{gid}_{uid}"
        if EFFECT_COUNT_FAIL in decision.effects:
            self.state.record_fail(key)
        if EFFECT_CLEAR_FAIL in decision.effects:
            self.state.clear_fail(key)
        if EFFECT_BLOCK in decision.effects:
            await self.add_to_block(gid, uid)
        return decision.approve, decision.reason, decision.code
