"""群友管理：信息展示（长文本自动分片）与清理群友。

清理流程：按“未发言天数 + 等级”筛候选 → 白名单跳过 → 分片文本确认 →
session_waiter 等“确认清理/取消清理” → 限流并发踢人。
同一群同一时刻只允许一个清理任务（群级互斥锁），避免多个管理员重复踢同一批人。
"""

import asyncio
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


class MemberHandle:
    def __init__(self, db: QQAdminDB, global_list: QQAdminGlobalList):
        self.db = db
        self.global_list = global_list
        self._clear_locks: dict[str, asyncio.Lock] = {}

    async def _fetch_members(self, event: AiocqhttpMessageEvent, group_id: str) -> list | None:
        """拉取群成员列表，失败时回执并返回 None"""
        try:
            return await event.bot.get_group_member_list(group_id=int(group_id))
        except Exception as e:
            logger.error(f"获取群成员列表失败：{e}")
            await event.send(event.plain_result(f"获取群成员信息失败：{e}"))
            return None

    async def _send_text(self, event: AiocqhttpMessageEvent, title: str, lines: list[str], chunk_size: int = 40, max_chars: int = 1500):
        """长文本按行数与字长双限分片发送，避免单条消息超出平台长度"""
        chunks: list[tuple[int, list[str]]] = []
        buf: list[str] = []
        cur_len = 0
        start = 0
        for idx, line in enumerate(lines):
            if buf and (len(buf) >= chunk_size or cur_len + len(line) + 1 > max_chars):
                chunks.append((start, buf))
                buf, cur_len, start = [], 0, idx
            buf.append(line)
            cur_len += len(line) + 1
        if buf:
            chunks.append((start, buf))

        total = len(lines)
        for i, (start, chunk) in enumerate(chunks):
            head = title if i == 0 else f"（续 {start + 1}-{start + len(chunk)}/{total}）"
            await event.send(event.plain_result(f"{head}\n" + "\n".join(chunk)))

    def _get_clear_lock(self, group_id: str) -> asyncio.Lock:
        """取本群清理任务的互斥锁（按群隔离，惰性创建）"""
        return self._clear_locks.setdefault(group_id, asyncio.Lock())

    async def get_group_member_list(self, event: AiocqhttpMessageEvent):
        """查看群友信息，长文本自动分片发送。"""
        await event.send(event.plain_result("获取中..."))
        members = await self._fetch_members(event, event.get_group_id())
        if members is None:
            return

        members = sorted(members, key=lambda m: _safe_int(m.get("join_time")))
        info_lines = [f"{format_time(_safe_int(m.get('join_time')))}：【{m.get('level', 0)}】{m.get('user_id', '?')}-{m.get('nickname', '（无昵称）')}" for m in members]
        if not info_lines:
            await event.send(event.plain_result("群内暂无成员数据"))
            return
        await self._send_text(event, "进群时间：【等级】QQ-昵称\n", info_lines)

    async def clear_group_member(self, event: AiocqhttpMessageEvent, inactive_days: int | None = None, under_level: int | None = None):
        """/清理群友 未发言天数 群等级（缺省用本群配置）"""
        lock = self._get_clear_lock(event.get_group_id())
        if lock.locked():
            await event.send(event.plain_result("本群已有清理任务正在进行，请等待其结束或取消后再试"))
            return

        async with lock:
            group_id = event.get_group_id()
            sender_id = event.get_sender_id()
            if inactive_days is None:
                inactive_days = await self.db.get(group_id, "clear_inactive_days", 30)
            if under_level is None:
                under_level = await self.db.get(group_id, "clear_under_level", 10)

            await event.send(event.plain_result("正在获取群友信息，请稍候……"))
            members = await self._fetch_members(event, group_id)
            if members is None:
                return

            threshold_ts = int(datetime.now().timestamp()) - inactive_days * 86400
            rows: list[tuple[int, int, str, str]] = []
            for m in members:
                last_sent = _safe_int(m.get("last_sent_time"))
                level = _safe_int(m.get("level"))
                if last_sent < threshold_ts and level < under_level:
                    rows.append((last_sent, level, m.get("user_id", ""), m.get("nickname", "（无昵称）")))

            # 白名单成员不清理（可通过“清理跳过白名单”配置关闭）
            skipped = 0
            if await self.db.get(group_id, "clear_skip_allow", True):
                allow_ids = {str(uid) for uid in await resolve_allow_ids(self.db, self.global_list, group_id)}
                kept = [r for r in rows if str(r[2]) not in allow_ids]
                skipped = len(rows) - len(kept)
                rows = kept

            if not rows:
                await event.send(event.plain_result(f"符合条件的 {skipped} 位均为白名单成员，已跳过" if skipped else "无符合条件的群友"))
                return

            rows.sort(key=lambda r: r[0])
            clear_ids = [r[2] for r in rows]
            info_lines = [f"{format_time(r[0])}｜{r[1]}级｜{r[2]} - {r[3]}" for r in rows]
            title = (
                f"共 {len(clear_ids)} 位群友符合清理条件（{inactive_days} 天内无发言且群等级低于 {under_level} 级）"
                + (f"，另有 {skipped} 位白名单成员已跳过" if skipped else "")
                + "，请发送「确认清理」或「取消清理」："
            )
            await self._send_text(event, title, info_lines)

            @session_waiter(timeout=60)  # type: ignore
            async def waiter(controller: SessionController, event: AiocqhttpMessageEvent):
                if group_id != event.get_group_id() or sender_id != event.get_sender_id():
                    return
                if event.message_str == "取消清理":
                    await event.send(event.plain_result("清理群友任务已取消"))
                    controller.stop()
                    return
                if event.message_str != "确认清理":
                    return

                await event.send(event.plain_result(f"正在执行清理（共 {len(clear_ids)} 位），请稍候……"))
                kick_semaphore = asyncio.Semaphore(KICK_CONCURRENCY)

                async def kick_one(clear_id):
                    name = str(clear_id)
                    try:
                        async with kick_semaphore:
                            name = await get_nickname(event, user_id=clear_id)
                            await event.bot.set_group_kick(group_id=int(group_id), user_id=int(clear_id), reject_add_request=False)
                            return f"✅ 已将 {name}({clear_id}) 踢出本群"
                    except Exception as e:
                        logger.error(f"踢出 {name}({clear_id}) 失败：{e}")
                        return f"❌ 踢出 {name}({clear_id}) 失败"

                # gather 保序，结果顺序与候选名单一致
                results = await asyncio.gather(*[kick_one(cid) for cid in clear_ids])
                if results:
                    await event.send(event.plain_result("\n".join(results)))
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
