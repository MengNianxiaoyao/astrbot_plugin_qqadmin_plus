"""群友管理：信息展示（按行分片）与清理群友。

清理流程：按“活跃时间（从未发言时取入群时间） + 等级”筛候选 → 白名单跳过 → 分片文本确认 →
session_waiter 等“确认清理/取消清理” → 限流并发踢人。
同一群同一时刻只允许一个清理任务（群级互斥锁），避免多个管理员重复踢同一批人。
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime

from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.utils.session_waiter import SessionController, session_waiter

from ..data import QQAdminDB, QQAdminGlobalList
from ..utils import format_time, get_nickname, resolve_allow_ids

# 确认清理阶段并发踢人的最大并发数
KICK_CONCURRENCY = 8


def _safe_int(value, default: int = 0) -> int:
    """尽力转 int，None/空串/非法值回 default"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _last_active_text(last_sent: int, now_ts: int) -> str:
    """把上次发言时间转成“从未发言 / N天前”，比绝对日期更易读"""
    if last_sent <= 0:
        return "从未发言"
    return f"{max(1, (now_ts - last_sent) // 86400)}天前"


@dataclass
class _Candidate:
    active_ts: int
    level: int
    user_id: str
    nickname: str
    active_text: str


class MemberHandle:
    def __init__(self, db: QQAdminDB, global_list: QQAdminGlobalList):
        self.db = db
        self.global_list = global_list
        self._clear_locks: dict[str, asyncio.Lock] = {}

    # ============================== 通用 ==============================

    async def _fetch_members(self, event: AiocqhttpMessageEvent, group_id: str) -> list | None:
        """拉取群成员列表，失败时回执并返回 None"""
        try:
            return await event.bot.get_group_member_list(group_id=int(group_id))
        except Exception as e:
            logger.error(f"获取群成员列表失败：{e}")
            await event.send(event.plain_result(f"获取群成员信息失败：{e}"))
            return None

    async def _send_text(self, event: AiocqhttpMessageEvent, title: str, lines: list[str], chunk_size: int = 50):
        """仅按行数分片发送：一行内容始终完整保留，绝不按字数截断"""
        total = len(lines)
        for start in range(0, total, chunk_size):
            chunk = lines[start : start + chunk_size]
            head = title if start == 0 else f"（续 {start + 1}-{start + len(chunk)}/{total}）"
            await event.send(event.plain_result(f"{head}\n" + "\n".join(chunk)))

    def _get_clear_lock(self, group_id: str) -> asyncio.Lock:
        """取本群清理任务的互斥锁（按群隔离，惰性创建）"""
        return self._clear_locks.setdefault(group_id, asyncio.Lock())

    @staticmethod
    def _format_member_line(member: dict) -> str:
        return f"{format_time(_safe_int(member.get('join_time')))}：【{member.get('level', 0)}】{member.get('user_id', '?')}-{member.get('nickname') or '（无昵称）'}"

    # ============================== 群友信息 ==============================

    async def get_group_member_list(self, event: AiocqhttpMessageEvent):
        """查看群友信息，按行分片发送。"""
        await event.send(event.plain_result("获取中..."))
        members = await self._fetch_members(event, event.get_group_id())
        if members is None:
            return
        if not members:
            await event.send(event.plain_result("群内暂无成员数据"))
            return

        members = sorted(members, key=lambda m: _safe_int(m.get("join_time")))
        await self._send_text(event, "进群时间：【等级】QQ-昵称", [self._format_member_line(m) for m in members])

    # ============================== 清理群友 ==============================

    @staticmethod
    def _collect_candidates(members: list, inactive_days: int, under_level: int, now_ts: int) -> list[_Candidate]:
        """按活跃时间与等级筛出候选；从未发言时以入群时间为准，避免误清刚入群成员"""
        threshold_ts = now_ts - inactive_days * 86400
        candidates: list[_Candidate] = []
        for member in members:
            last_sent = _safe_int(member.get("last_sent_time"))
            active_ts = last_sent if last_sent > 0 else _safe_int(member.get("join_time"))
            level = _safe_int(member.get("level"))
            if active_ts < threshold_ts and level < under_level:
                candidates.append(
                    _Candidate(
                        active_ts=active_ts,
                        level=level,
                        user_id=str(member.get("user_id", "")),
                        nickname=member.get("nickname") or "（无昵称）",
                        active_text=_last_active_text(last_sent, now_ts),
                    )
                )
        return candidates

    async def _filter_allowlist(self, group_id: str, candidates: list[_Candidate]) -> tuple[list[_Candidate], int]:
        """去掉白名单成员，返回剩余候选与跳过数量"""
        if not await self.db.get(group_id, "clear_skip_allow", True):
            return candidates, 0
        allow_ids = {str(uid) for uid in await resolve_allow_ids(self.db, self.global_list, group_id)}
        kept = [c for c in candidates if c.user_id not in allow_ids]
        return kept, len(candidates) - len(kept)

    @staticmethod
    def _format_candidates(candidates: list[_Candidate]) -> list[str]:
        return [f"{idx}. {c.nickname}({c.user_id})｜{c.level}级｜{c.active_text}" for idx, c in enumerate(candidates, 1)]

    @staticmethod
    def _build_title(candidates: list[_Candidate], skipped: int, inactive_days: int, under_level: int) -> str:
        title = f"共 {len(candidates)} 位群友符合清理条件（{inactive_days} 天未发言且等级低于 {under_level} 级）"
        if skipped:
            title += f"，已跳过 {skipped} 位白名单"
        return title + "。\n发送「确认清理」执行，或「取消清理」放弃："

    async def _kick_one(self, event: AiocqhttpMessageEvent, group_id: str, semaphore: asyncio.Semaphore, candidate: _Candidate) -> bool:
        name = candidate.nickname
        try:
            async with semaphore:
                name = await get_nickname(event, user_id=candidate.user_id)
                await event.bot.set_group_kick(group_id=int(group_id), user_id=int(candidate.user_id), reject_add_request=False)
                return True
        except Exception as e:
            logger.error(f"踢出 {name}({candidate.user_id}) 失败：{e}")
            return False

    async def _kick_all(self, event: AiocqhttpMessageEvent, group_id: str, candidates: list[_Candidate]) -> tuple[int, int]:
        """并发踢人，返回（成功数, 失败数）"""
        semaphore = asyncio.Semaphore(KICK_CONCURRENCY)
        results = await asyncio.gather(*[self._kick_one(event, group_id, semaphore, c) for c in candidates])
        succeeded = sum(results)
        return succeeded, len(results) - succeeded

    async def _confirm_and_kick(self, event: AiocqhttpMessageEvent, group_id: str, sender_id: str, candidates: list[_Candidate]) -> None:
        """等待“确认清理/取消清理”，确认后执行踢人并回执汇总"""

        @session_waiter(timeout=60)  # type: ignore
        async def waiter(controller: SessionController, wait_event: AiocqhttpMessageEvent):
            if group_id != wait_event.get_group_id() or sender_id != wait_event.get_sender_id():
                return
            if wait_event.message_str == "取消清理":
                await wait_event.send(wait_event.plain_result("清理群友任务已取消"))
                controller.stop()
                return
            if wait_event.message_str != "确认清理":
                return

            await wait_event.send(wait_event.plain_result(f"正在执行清理（共 {len(candidates)} 位），请稍候……"))
            succeeded, failed = await self._kick_all(wait_event, group_id, candidates)
            summary = f"清理完成：成功 {succeeded} 位，失败 {failed} 位。"
            await wait_event.send(wait_event.plain_result(summary))
            controller.stop()

        try:
            await waiter(event)
        except TimeoutError:
            await event.send(event.plain_result("等待超时！"))
        except Exception as e:
            logger.error("清理群友任务出错: " + str(e))
            await event.send(event.plain_result(f"清理群友任务出错：{e}"))
        finally:
            event.stop_event()

    async def clear_group_member(self, event: AiocqhttpMessageEvent, inactive_days: int | None = None, under_level: int | None = None):
        """清理群友 [未发言天数] [群等级]，缺省用本群配置"""
        group_id = event.get_group_id()
        lock = self._get_clear_lock(group_id)
        if lock.locked():
            await event.send(event.plain_result("本群已有清理任务正在进行，请等待其结束或取消后再试"))
            return

        async with lock:
            sender_id = event.get_sender_id()
            if inactive_days is None:
                inactive_days = await self.db.get(group_id, "clear_inactive_days", 30)
            if under_level is None:
                under_level = await self.db.get(group_id, "clear_under_level", 10)

            await event.send(event.plain_result("正在获取群友信息，请稍候……"))
            members = await self._fetch_members(event, group_id)
            if members is None:
                return

            candidates = self._collect_candidates(members, inactive_days, under_level, int(datetime.now().timestamp()))
            candidates, skipped = await self._filter_allowlist(group_id, candidates)
            if not candidates:
                await event.send(event.plain_result(f"符合条件的 {skipped} 位均为白名单成员，已跳过" if skipped else "无符合条件的群友"))
                return

            candidates.sort(key=lambda c: c.active_ts)
            await self._send_text(event, self._build_title(candidates, skipped, inactive_days, under_level), self._format_candidates(candidates))
            await self._confirm_and_kick(event, group_id, sender_id, candidates)
