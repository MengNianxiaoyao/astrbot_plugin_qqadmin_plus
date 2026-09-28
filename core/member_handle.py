from __future__ import annotations

import asyncio
from datetime import datetime

from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.utils.session_waiter import SessionController, session_waiter

from ..data import QQAdminDB, QQAdminGlobalList
from ..utils import format_time, get_nickname, resolve_allow_ids

# 候选名单超过此行数时跳过图片渲染，直接走文本分片（大图渲染慢且易失败）
CANDIDATE_IMAGE_MAX_ROWS = 100
# 确认清理阶段并发踢人的最大并发数
KICK_CONCURRENCY = 8


class MemberHandle:
    def __init__(self, db: QQAdminDB, global_list: QQAdminGlobalList, text_to_image):
        self.db = db
        self.global_list = global_list
        self.text_to_image = text_to_image

    async def get_group_member_list(self, event: AiocqhttpMessageEvent):
        """查看群友信息。优先生成图片展示，图片失败时自动降级为分片文本。"""
        await event.send(event.plain_result("获取中..."))
        group_id = event.get_group_id()
        try:
            members_data = await event.bot.get_group_member_list(group_id=int(group_id))
        except Exception as e:
            logger.error(f"获取群成员列表失败：{e}")
            await event.send(event.plain_result(f"获取群成员信息失败：{e}"))
            return

        def _join_time(member):
            try:
                return int(member.get("join_time", 0) or 0)
            except (TypeError, ValueError):
                return 0

        info_lines = [f"{format_time(_join_time(m))}：【{m.get('level', 0)}】{m.get('user_id', '?')}-{m.get('nickname', '（无昵称）')}" for m in sorted(members_data, key=_join_time)]

        if not info_lines:
            await event.send(event.plain_result("群内暂无成员数据"))
            return

        header = "进群时间：【等级】QQ-昵称\n\n"
        try:
            url = await self.text_to_image(header + "\n\n".join(info_lines))
            if not url:
                raise RuntimeError("text_to_image 未返回图片地址")
            await event.send(event.image_result(url))
        except Exception as e:
            logger.warning(f"生成群成员列表图片失败，回退为文本发送：{e}")
            await self._send_text_fallback(event, header, info_lines)

    async def _send_text_fallback(self, event: AiocqhttpMessageEvent, title: str, lines: list[str], chunk_size: int = 40, max_chars: int = 1500):
        """图片生成失败时，将长文本分片发送，避免单条消息超出平台长度限制（按行数与字长双限）。"""
        total = len(lines)
        # 预估单条不超限直接发
        if total <= chunk_size and len(title) + sum(len(line) + 1 for line in lines) <= max_chars:
            await event.send(event.plain_result(title + "\n" + "\n".join(lines)))
            return
        buf: list[str] = []
        cur_len = len(title)
        start_idx = 0
        for idx, line in enumerate(lines):
            if len(buf) >= chunk_size or cur_len + len(line) + 1 > max_chars:
                head = title if start_idx == 0 else f"（续 {start_idx + 1}-{start_idx + len(buf)}/{total}）"
                await event.send(event.plain_result(f"{head}\n" + "\n".join(buf)))
                start_idx += len(buf)
                buf = []
                cur_len = 0
            buf.append(line)
            cur_len += len(line) + 1
        if buf:
            head = title if start_idx == 0 else f"（续 {start_idx + 1}-{total}/{total}）"
            await event.send(event.plain_result(f"{head}\n" + "\n".join(buf)))

    async def clear_group_member(
        self,
        event: AiocqhttpMessageEvent,
        inactive_days: int | None = None,
        under_level: int | None = None,
    ):
        """/清理群友 未发言天数 群等级（缺省用本群配置）"""
        group_id = event.get_group_id()
        sender_id = event.get_sender_id()

        if inactive_days is None:
            inactive_days = await self.db.get(group_id, "clear_inactive_days", 30)
        if under_level is None:
            under_level = await self.db.get(group_id, "clear_under_level", 10)

        try:
            members_data = await event.bot.get_group_member_list(group_id=int(group_id))
        except Exception as e:
            await event.send(event.plain_result(f"获取群成员信息失败：{e}"))
            return

        threshold_ts = int(datetime.now().timestamp()) - inactive_days * 86400
        rows: list[tuple[int, int, str, str]] = []

        for member in members_data:  # type: ignore
            try:
                last_sent = int(member.get("last_sent_time", 0) or 0)
            except (TypeError, ValueError):
                last_sent = 0
            try:
                level = int(member.get("level", 0))
            except (TypeError, ValueError):
                level = 0
            user_id = member.get("user_id", "")
            nickname = member.get("nickname", "（无昵称）")

            if last_sent < threshold_ts and level < under_level:
                rows.append((last_sent, level, user_id, nickname))

        # 白名单成员不清理（可通过“清理跳过白名单”配置关闭）
        skipped = 0
        if await self.db.get(group_id, "clear_skip_allow", True):
            allow_ids = {str(uid) for uid in await resolve_allow_ids(self.db, self.global_list, group_id)}
            skipped = sum(1 for row in rows if str(row[2]) in allow_ids)
            rows = [row for row in rows if str(row[2]) not in allow_ids]

        if not rows:
            if skipped:
                await event.send(event.plain_result(f"符合条件的 {skipped} 位均为白名单成员，已跳过"))
            else:
                await event.send(event.plain_result("无符合条件的群友"))
            return

        # 按发言时间排序（直接按时间戳排序，不再反解析格式化字符串）
        rows.sort(key=lambda row: row[0])

        clear_ids: list[int] = []
        info_lines: list[str] = []
        for last_sent, level, user_id, nickname in rows:
            clear_ids.append(user_id)
            info_lines.append(f"- **{format_time(last_sent)}**｜**{level}**级｜`{user_id}` - {nickname}")

        info_str = (
            f"### 共 **{len(clear_ids)}** 位群友 **{inactive_days}** 天内无发言，群等级低于 **{under_level}** 级\n\n"
            + "\n".join(info_lines)
            + (f"\n\n（另有 {skipped} 位白名单成员已跳过）" if skipped else "")
            + "\n\n### 请发送 **确认清理** 或 **取消清理** 来处理这些群友！"
        )

        fallback_title = (
            f"共 {len(clear_ids)} 位群友符合清理条件（{inactive_days} 天内无发言且群等级低于 {under_level} 级）"
            + (f"，另有 {skipped} 位白名单成员已跳过" if skipped else "")
            + "，请发送「确认清理」或「取消清理」："
        )
        # 候选人太多时图片渲染慢且易失败，直接走文本分片
        if len(info_lines) > CANDIDATE_IMAGE_MAX_ROWS:
            await self._send_text_fallback(event, fallback_title, info_lines)
        else:
            try:
                url = await self.text_to_image(info_str)
                if not url:
                    raise RuntimeError("text_to_image 未返回图片地址")
                await event.send(event.image_result(url))
            except Exception as e:
                logger.warning(f"生成清理候选图片失败，回退为文本发送：{e}")
                await self._send_text_fallback(event, fallback_title, info_lines)

        @session_waiter(timeout=60)  # type: ignore
        async def empty_mention_waiter(controller: SessionController, event: AiocqhttpMessageEvent):
            if group_id != event.get_group_id() or sender_id != event.get_sender_id():
                return

            if event.message_str == "取消清理":
                await event.send(event.plain_result("清理群友任务已取消"))
                controller.stop()
                return

            if event.message_str == "确认清理":
                await event.send(event.plain_result(f"正在执行清理（共 {len(clear_ids)} 位），请稍候……"))
                kick_semaphore = asyncio.Semaphore(KICK_CONCURRENCY)

                async def _kick_one(clear_id):
                    try:
                        async with kick_semaphore:
                            target_name = await get_nickname(event, user_id=clear_id)
                            await event.bot.set_group_kick(
                                group_id=int(group_id),
                                user_id=int(clear_id),
                                reject_add_request=False,
                            )
                            return f"✅ 已将 {target_name}({clear_id}) 踢出本群"
                    except Exception as e:
                        logger.error(f"踢出 {target_name}({clear_id}) 失败：{e}")
                        return f"❌ 踢出 {target_name}({clear_id}) 失败"

                # gather 保序，结果顺序与候选名单一致
                msg_list = list(await asyncio.gather(*[_kick_one(cid) for cid in clear_ids]))

                if msg_list:
                    await event.send(event.plain_result("\n".join(msg_list)))
                controller.stop()

        try:
            await empty_mention_waiter(event)
        except TimeoutError as _:
            await event.send(event.plain_result("等待超时！"))
        except Exception as e:
            logger.error("清理群友任务出错: " + str(e))
        finally:
            event.stop_event()
