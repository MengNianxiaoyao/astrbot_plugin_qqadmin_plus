import asyncio
import time

from astrbot.api import logger

from ...data import QQAdminDB, QQAdminGlobalList
from ...utils import resolve_allow_ids, resolve_block_ids
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
        # -1.群人数已满时直接拒绝（可通过“群满拒绝”命令关闭）
        if await self.db.get(gid, "join_full_reject", True):
            if await self._is_group_full(gid, client):
                full_msg = await self.db.get(gid, "join_full_msg", "群人数已满")
                return False, full_msg or "群人数已满", "full"

        # -0.禁止多群加入（可通过配置关闭）
        if await self.db.get(gid, "join_single_group", False):
            other = await self._find_other_group(gid, uid, client)
            if other:
                return False, f"已加入其他群聊({other})", "multi_group"
            earlier = self.state.find_earlier_application(uid, gid)
            if earlier:
                return False, f"已申请其他群聊({earlier})", "multi_group"

        # 0.白名单用户直接通过
        allow_ids = await resolve_allow_ids(self.db, self.global_list, gid)
        if uid in allow_ids:
            return True, "白名单用户", "allow"

        # 1.黑名单用户
        block_ids = await resolve_block_ids(self.db, self.global_list, gid)
        if uid in block_ids:
            return False, "黑名单用户", "block"

        # 2.QQ等级过低或隐藏
        if user_level is None:
            return None, "QQ等级可能被隐藏，人工审核", "level_hidden"

        min_level = await self.db.get(gid, "join_min_level", 8)
        if min_level > 0 and user_level is not None and user_level < min_level:
            return False, f"QQ等级过低({user_level}<{min_level})", "level_low"

        if comment:
            # 提取答案部分
            keyword = "\n答案："
            if keyword in comment:
                comment = comment.split(keyword, 1)[1]

        if not comment:
            if await self.db.get(gid, "join_no_match_msg", False):
                return False, "验证信息为空", "empty_msg"
        else:
            lower_comment = comment.lower()
            # 3.命中进群黑词
            rkws = await self.db.get(gid, "join_reject_words", [])
            if any(rk.lower() in lower_comment for rk in rkws):
                if await self.db.get(gid, "reject_word_block", False):
                    await self.add_to_block(gid, uid)
                    return False, "命中进群黑词，已拉黑", "black_word_block"
                return False, "命中进群黑词", "black_word"

            # 4.命中进群白词
            akws = await self.db.get(gid, "join_accept_words", [])
            if akws and any(ak.lower() in lower_comment for ak in akws):
                return True, "命中进群白词", "white_word"

        # 5.最大失败次数（内存防爆破，带24h过期）
        max_fail = await self.db.get(gid, "join_max_time", 3)
        if max_fail > 0:
            key = f"{gid}_{uid}"
            now = time.time()
            # 过期清理
            if key in self.state.fail_time and now - self.state.fail_time[key] > 86400:
                self.state.fail.pop(key, None)
            self.state.fail[key] = self.state.fail.get(key, 0) + 1
            self.state.fail_time[key] = now
            if self.state.fail[key] > max_fail:
                await self.add_to_block(gid, uid)
                self.state.fail.pop(key, None)
                self.state.fail_time.pop(key, None)
                return False, f"进群尝试次数已达上限({max_fail}次)，已拉黑", "max_fail"

        # 6.未命中白词时, 自动驳回
        if await self.db.get(gid, "join_no_match_reject"):
            return False, "未命中进群关键词", "no_match"

        # 7.未命中进群关键词, 人工审核
        return None, "人工审核", "manual"
