import asyncio
import time

from astrbot.api import logger

from ...data import QQAdminDB, QQAdminGlobalList
from ...utils import (
    TTLCache,
    get_group_member,
    resolve_allow_ids,
    resolve_block_ids,
    safe_int,
    unwrap_data,
)
from .decision import (
    EFFECT_BLOCK,
    EFFECT_CLEAR_FAIL,
    EFFECT_COUNT_FAIL,
    Applicant,
    GroupJoinSnapshot,
    decide,
)
from .state import PENDING_DEFAULT_DAYS, JoinState, pending_ttl_secs

_MISSING = object()


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
        # 多群探测结果短缓存：同一申请人的重复事件不重复扫全群
        self._other_group_cache = TTLCache(ttl=60, capacity=500)

    async def _group_counts(self, gid: str, client=None) -> tuple[int, int] | None:
        """群（现有人数, 上限）。必须实时直查 OneBot 接口，不用群缓存（待审核名额差一两人都不能错）。
        拿不到有效人数时返回 None（调用方按放行处理）。"""

        def _counts(info: dict | None) -> tuple[int, int] | None:
            info = unwrap_data(info)
            if not isinstance(info, dict):
                return None
            member_count = safe_int(info.get("member_count"), 0)
            max_count = safe_int(info.get("max_member_count"), 0)
            if member_count <= 0 or max_count <= 0:
                return None
            return member_count, max_count

        # 实时直查 OneBot 接口（带 no_cache，避免实现端缓存）
        if client is None:
            return None
        try:
            try:
                info = await client.get_group_info(group_id=int(gid), no_cache=True)
            except TypeError:
                # 部分 OneBot 实现不认 no_cache 参数
                info = await client.get_group_info(group_id=int(gid))
            if counts := _counts(info):
                return counts
        except Exception as e:
            logger.debug(f"获取群 {gid} 人数失败，跳过满群检查: {e}")

        return None

    async def _reconcile_pending_by_membership(self, gid: str, client, ttl: float) -> int:
        """按成员关系对账：待定者若已在群里（站外批准但事件漏掉），清掉其登记。
        探测失败按“不在群里”处理，只清确定已进群的。返回清除条数。"""
        if client is None:
            return 0
        uids: list[str] = []
        for _, r in self.state.fresh_entries(gid, ttl=ttl):
            uid = str(r.get("uid") or "")
            if uid and uid not in uids:
                uids.append(uid)
        if not uids:
            return 0

        semaphore = asyncio.Semaphore(8)

        async def _is_member(uid: str) -> str | None:
            async with semaphore:
                info = await get_group_member(client, gid, uid, timeout_secs=10, no_cache=True)
            return uid if info else None

        cleared = 0
        for uid in await asyncio.gather(*[_is_member(u) for u in uids]):
            if uid:
                cleared += self.state.drop_pending(lambda k, r, uid=uid: r.get("gid") == gid and r.get("uid") == uid)
        return cleared

    async def _find_other_group(self, gid: str, uid: str, client=None) -> str | None:
        """检查用户是否已在同账号管理的其他群中，返回群名，否返回 None。"""
        if client is None or not self._group_cache:
            return None
        cache_key = (gid, uid)
        cached = self._other_group_cache.get(cache_key, _MISSING)
        if cached is not _MISSING:
            return cached
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
            self._other_group_cache.set(cache_key, None)
            return None

        semaphore = asyncio.Semaphore(4)

        async def _check(item: tuple[str, str]) -> str | None:
            ngid, name = item
            # 单次探测超时封顶：部分 OneBot 实现在查询非成员时可能无响应
            async with semaphore:
                info = await get_group_member(client, ngid, uid, timeout_secs=10, no_cache=True)
            # 探测失败按“不在该群”处理，不阻断正常审核
            return name if info else None

        # 并发探测但限流；找到第一个就取消剩余请求，群多时不至于一直等最慢的
        result: str | None = None
        tasks = [asyncio.ensure_future(_check(item)) for item in others]
        try:
            for future in asyncio.as_completed(tasks):
                name = await future
                if name:
                    result = name
                    break
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        self._other_group_cache.set(cache_key, result)
        return result

    async def add_to_block(self, gid: str, uid: str):
        """向群黑名单或全局黑名单添加用户（根据 use_global_block 判断）"""
        if await self.db.get(gid, "use_global_block", False):
            await self.global_list.add("block", uid)
        else:
            await self.db.add(gid, "block_ids", uid)

    async def _load_snapshot(self, gid: str, uid: str, client=None) -> GroupJoinSnapshot:
        """一次加载判定所需的全部输入（含条件 IO：开关关闭时不触发网络探测）。"""
        full_reject = await self.db.get(gid, "join_full_reject", True)
        single_group = await self.db.get(gid, "join_single_group", False)
        other = await self._find_other_group(gid, uid, client) if single_group else None
        earlier = self.state.find_earlier_application(uid, gid) if single_group and not other else None
        max_fail = await self.db.get(gid, "join_max_time", 3)
        counts = await self._group_counts(gid, client) if full_reject else None
        slots_left = counts[1] - counts[0] if counts else None
        pending_ttl = pending_ttl_secs(await self.db.get(gid, "join_pending_ttl", PENDING_DEFAULT_DAYS))
        pending_count = len(self.state.fresh_entries(gid, ttl=pending_ttl))
        if full_reject and slots_left is not None and pending_count + 1 > slots_left and client is not None:
            # 真要超限驳回前先对账：把已在群里的待定者清掉重算，避免站外批准漏事件导致误拦。
            # 平时不触发，本次申请零额外开销。
            await self._reconcile_pending_by_membership(gid, client, pending_ttl)
            pending_count = len(self.state.fresh_entries(gid, ttl=pending_ttl))
        return GroupJoinSnapshot(
            join_full_reject=full_reject,
            join_full_msg=await self.db.get(gid, "join_full_msg", "群人数已满"),
            is_full=counts is not None and counts[0] >= counts[1],
            join_pending_msg=await self.db.get(gid, "join_pending_msg", "待审核人数已超过剩余名额"),
            slots_left=counts[1] - counts[0] if counts else None,
            pending_count=pending_count,
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
        / max_fail超次已拉黑 / no_match未命中驳回 / pending_full待审核超限 / manual人工审核
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
