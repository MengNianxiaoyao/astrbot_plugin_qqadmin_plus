"""抽奖引擎：创建、定时开奖、发奖与跨群通知。"""

import asyncio
import random
import time
from typing import Any

from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .models import MAX_HISTORY, Lottery, finished_reason, instant_chance
from .parse import clock_label
from .render import result_lines, summary_lines


class LotteryConflictError(Exception):
    """创建时选中的群已有进行中的抽奖。"""

    def __init__(self, groups: list[str]):
        self.groups = groups
        super().__init__("、".join(groups))


class LotteryNotReadyError(Exception):
    """开奖前平台连接未就绪：整场推迟，等连接恢复后再开。"""


# 连接未就绪时重新尝试开奖的间隔（秒），与 client_for 的静默窗口一致
DRAW_RETRY_DELAY = 10.0


class LotteryEngine:
    def __init__(self, handle):
        self.handle = handle
        self.tasks: dict[str, asyncio.Task] = {}
        self.bg_tasks: set[asyncio.Task] = set()
        self._draw_retries: dict[str, asyncio.Task] = {}
        self._client_retry_at = 0.0

    # ============================== 任务 ==============================

    def schedule(self, lottery: Lottery):
        task = asyncio.create_task(self.wait_and_draw(lottery))
        self.tasks[lottery.group_id] = task
        task.add_done_callback(lambda _t, gid=lottery.group_id: self.tasks.pop(gid, None))

    def forget_task(self, group_id: str):
        task = self.tasks.pop(group_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.bg_tasks.add(task)
        task.add_done_callback(self._bg_done)

    def _bg_done(self, task: asyncio.Task):
        self.bg_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(f"抽奖后台任务异常: {exc}", exc_info=exc)

    def cancel_all(self):
        for task in [*self.tasks.values(), *self.bg_tasks, *self._draw_retries.values()]:
            task.cancel()
        self.tasks.clear()
        self.bg_tasks.clear()
        self._draw_retries.clear()

    # ============================== 创建 ==============================

    async def create(self, sender_id: str, data: dict[str, Any], client: Any) -> Lottery:
        busy = [gid for gid in data["groups"] if gid in self.handle.active]
        if busy:
            raise LotteryConflictError(busy)

        expire_at = data["expire_at"]
        duration = data.get("duration")
        label = data["label"]
        deadline_label = data.get("deadline_label", "")
        if duration:
            # 倒计时从创建完成起算，避免多步问答本身吃掉时间
            expire_at = time.time() + duration
            if data["mode"] == "time":
                label = clock_label(expire_at, self.handle.tz, "开奖")
        elif expire_at is not None and expire_at <= time.time():
            # HH:MM 在问答过程中已过点：顺延到次日同一时间，并同步公告时间
            expire_at += 86400
            if data["mode"] == "time":
                label = clock_label(expire_at, self.handle.tz, "开奖")
            else:
                deadline_label = clock_label(expire_at, self.handle.tz, "截止")

        # 私聊发起没有当前群，用第一个参与群作为主群（定时任务与日志使用）
        primary_group = data["groups"][0]
        lottery = Lottery(
            group_id=primary_group,
            groups=data["groups"],
            prize=data["prize"],
            copies=data["copies"],
            mode=data["mode"],
            starter=sender_id,
            label=label,
            delivery=data["delivery"],
            stock=list(data["stock"]),
            min_level=data["min_level"],
            titles=data["titles"],
            threshold_all=data.get("threshold_all", False),
            expire_at=expire_at,
            target_count=data["target_count"],
            max_participants=data["max_participants"],
            deadline_label=deadline_label,
            bot_id=str(data.get("bot_id") or ""),
            client=client,
        )
        for gid in lottery.groups:
            self.handle.active[gid] = lottery
        if lottery.expire_at:
            self.schedule(lottery)
        return lottery

    async def announce_start(self, event: AiocqhttpMessageEvent, lottery: Lottery):
        lines = [
            "【抽奖开始】",
            *summary_lines(lottery),
        ]
        if lottery.mode == "instant":
            if lottery.max_participants:
                lines.append(f"参与人数上限：{lottery.max_participants}（中奖概率 {instant_chance(lottery):.2%}）")
            if lottery.deadline_label:
                lines.append(f"参与截止：{lottery.deadline_label}")
            lines.append("每次参与立即随机判定，名额抽完、人数达上限或到截止时间即结束")
        lines.append("发送「/参与抽奖」参加；同一时间每群仅一场抽奖")
        text = "\n".join(lines)
        await event.send(event.plain_result(f"抽奖已创建，开始公告已发送到参与群：{'、'.join(lottery.groups)}"))
        await self.broadcast(lottery, text)

    # ============================== 参与 / 开奖 ==============================

    async def instant_draw(
        self, event: AiocqhttpMessageEvent, lottery: Lottery, user_id: str, nickname: str
    ) -> list[str]:
        if lottery.closed:
            return []
        cap = lottery.max_participants or 0
        if len(lottery.winners) >= lottery.copies:
            # 并发参与时名额可能刚被抽完，不能再追加中奖者
            texts = ["本次抽奖奖品已抽完，正在开奖"]
            texts += await self.safe_draw(lottery, event=event, reason="名额已抽完")
            return texts
        chance = instant_chance(lottery) if cap else 1.0
        if random.random() < chance:
            lottery.winners.append((user_id, nickname))
            remaining = lottery.copies - len(lottery.winners)
            tail = self._take_pending_item(lottery, user_id)
            texts = [f"恭喜 {nickname} 中奖：{lottery.prize}！{tail}（剩余 {remaining} 份）"]
        else:
            remaining = max(0, lottery.copies - len(lottery.winners))
            texts = [
                f"{nickname} 未中奖；本次抽奖已参与，不能重复参与"
                f"（{len(lottery.participants)}/{cap} 人，剩余 {remaining} 份）"
            ]
        if reason := finished_reason(lottery):
            texts += await self.safe_draw(lottery, event=event, reason=reason)
        return texts

    async def wait_and_draw(self, lottery: Lottery):
        delay = max(0.0, (lottery.expire_at or time.time()) - time.time())
        try:
            await asyncio.sleep(delay)
            reason = lottery.label if lottery.mode == "time" else "已到截止时间"
            await self.safe_draw(lottery, reason=reason)
        except asyncio.CancelledError:
            raise

    async def safe_draw(
        self, lottery: Lottery, event: AiocqhttpMessageEvent | None = None, reason: str = ""
    ) -> list[str]:
        """开奖兜底：连接未就绪时整场推迟；异常时回滚闭锁状态，避免永久卡在“正在开奖”。"""
        try:
            return await self.draw(lottery, event=event, reason=reason)
        except LotteryNotReadyError:
            self.schedule_draw_retry(lottery, reason)
            logger.warning(f"抽奖开奖推迟：平台连接未就绪（{lottery.group_id}）")
            # 平台未就绪时任何回复都发不出去，等重试成功后统一发开奖公告
            return []
        except Exception as e:
            logger.error(f"抽奖开奖失败({lottery.group_id}): {e}", exc_info=True)
            lottery.closed = False
            lottery.closing = False
            await self.handle.save(immediate=True)
            return []

    def schedule_draw_retry(self, lottery: Lottery, reason: str):
        """连接未就绪时安排一次延迟重试，同一场抽奖只保留一个重试任务。"""
        gid = lottery.group_id
        existing = self._draw_retries.get(gid)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self._retry_draw(lottery, reason))
        self._draw_retries[gid] = task
        task.add_done_callback(lambda _t, gid=gid: self._draw_retries.pop(gid, None))

    async def _retry_draw(self, lottery: Lottery, reason: str):
        await asyncio.sleep(DRAW_RETRY_DELAY)
        if lottery.closed:
            return
        # 重试必须真正探测一次，不能被上次失败设置的静默窗口挡住
        self._client_retry_at = 0.0
        await self.safe_draw(lottery, reason=reason)

    async def cancel_draw(self, lottery: Lottery):
        """取消抽奖：不抽奖、不发奖、不写历史；同时停掉定时与延迟重试任务。"""
        if lottery.closed:
            return
        lottery.closed = True
        lottery.closing = False
        for gid in lottery.groups:
            if self.handle.active.get(gid) is lottery:
                self.handle.active.pop(gid, None)
        self.forget_task(lottery.group_id)
        retry = self._draw_retries.pop(lottery.group_id, None)
        if retry is not None:
            retry.cancel()
        await self.handle.save(immediate=True)

    async def draw(self, lottery: Lottery, event: AiocqhttpMessageEvent | None = None, reason: str = "") -> list[str]:
        if lottery.closed:
            return []
        # 开奖前先确认连接可用；未就绪不闭锁、不写历史，整场推迟到重试
        if not await self.client_for(lottery):
            lottery.closing = True
            await self.handle.save()
            raise LotteryNotReadyError(lottery.group_id)
        lottery.closed = True
        lottery.closing = False
        lottery.drawn_at = time.time()
        lottery.participant_count = len(lottery.participants)

        if lottery.mode != "instant":
            # 时间/人数开奖：从全部参与者中随机抽取，份数多于参与人数时全员中奖
            candidates = list(lottery.participants)
            winners = random.sample(candidates, k=min(lottery.copies, len(candidates)))
            lottery.winners = [(uid, lottery.participants[uid]) for uid in winners]
            self._assign_pending_items(lottery)

        # 开奖后历史只需要中奖明细：清空参与者与剩余库存，只保留中奖者所在的群
        winner_ids = {uid for uid, _ in lottery.winners}
        lottery.participants.clear()
        lottery.stock.clear()
        lottery.user_groups = {uid: gid for uid, gid in lottery.user_groups.items() if uid in winner_ids}

        for gid in lottery.groups:
            if self.handle.active.get(gid) is lottery:
                self.handle.active.pop(gid, None)
            self.handle.last[gid] = lottery
            records = self.handle.history.setdefault(gid, [])
            records.insert(0, lottery)
            del records[MAX_HISTORY:]
        self.forget_task(lottery.group_id)
        await self.handle.save(immediate=True)
        return await self.announce_result(lottery, event=event, reason=reason)

    # ============================== 发奖 / 通知 ==============================

    async def client_for(self, lottery: Lottery):
        if lottery.client is not None:
            return lottery.client
        cache = self.handle.group_cache
        if cache is None:
            logger.warning(f"抽奖没有群缓存，无法获取 aiocqhttp client: {lottery.group_id}")
            return None
        if time.time() < self._client_retry_at:
            return None

        # adapter 存在不代表 WebSocket 已连上：每轮限时发现并按 bot_id 匹配登录信息，
        # 最多约 3 秒；拿不到就跳过本次通知，30 秒内不再重试，避免拖慢开奖。
        client = None
        for attempt in range(3):
            if attempt:
                await asyncio.sleep(0.4)
            try:
                client = await asyncio.wait_for(
                    cache.get_client(bot_id=lottery.bot_id, group_id=lottery.group_id),
                    timeout=0.8,
                )
            except Exception as e:
                logger.debug(f"抽奖探测 aiocqhttp 连接失败: {e}")
                client = None
            if client is None:
                continue
            # 旧数据没有 bot_id，get_client 无法按登录信息确认，补一次轻量探测
            if not lottery.bot_id and not await self._client_ready(client):
                client = None
                continue
            break

        if client is None:
            self._client_retry_at = time.time() + 10
            logger.warning(f"抽奖没有可用的 aiocqhttp 连接（bot {lottery.bot_id or '未知'}）")
            return None
        self._client_retry_at = 0.0
        lottery.client = client
        return client

    @staticmethod
    async def _client_ready(client) -> bool:
        """轻量就绪探测：能拿到登录信息才认为连接真正可用。"""
        try:
            await asyncio.wait_for(client.get_login_info(), timeout=0.6)
            return True
        except Exception:
            return False

    @staticmethod
    def _assign_pending_items(lottery: Lottery):
        """预存奖品按中奖顺序记到中奖者名下，等本人私聊「领奖」；不再主动私聊。"""
        if lottery.delivery != "private":
            return
        items = lottery.stock
        lottery.stock = []
        for idx, (uid, _name) in enumerate(lottery.winners):
            if idx < len(items) and items[idx]:
                lottery.pending_items[uid] = items[idx]

    @staticmethod
    def _take_pending_item(lottery: Lottery, user_id: str) -> str:
        """即抽即开命中后取一份预存奖品记到名下，返回追加到提示后的说明。"""
        if lottery.delivery != "private":
            return "请联系发起人领取"
        item = lottery.stock.pop(0) if lottery.stock else ""
        if not item:
            return "请联系发起人领取"
        lottery.pending_items[user_id] = item
        return "预存奖品请私聊机器人发送「/领奖」"

    async def announce_result(
        self, lottery: Lottery, event: AiocqhttpMessageEvent | None = None, reason: str = ""
    ) -> list[str]:
        """返回给调用方 yield 的文本；当前群由入口解析 CQ at 码，其余群广播同内容。"""
        empty_text = "本次无人中奖" if lottery.participant_count else "无人参与，本次抽奖取消"
        text = "\n".join(
            result_lines(
                lottery,
                self.handle.tz,
                "【抽奖开奖】",
                reason=reason,
                show_drawn_at=False,
                empty_text=empty_text,
                remaining_hint=True,
                mention_winners=True,
            )
        )
        if event is None:
            await self.broadcast(lottery, text)
            return []
        # 触发开奖的群由调用方 yield，其余参与群通过 client 广播
        await self.broadcast(lottery, text, skip={event.get_group_id()})
        return [text]

    async def broadcast(self, lottery: Lottery, text: str, skip: set[str] | None = None):
        """向除 skip 外的所有参与群发送同一份通知。"""
        remaining = [gid for gid in lottery.groups if gid not in (skip or ())]
        if not remaining:
            return
        client = await self.client_for(lottery)
        if client is None:
            logger.warning(f"抽奖通知缺少可用 client，已跳过发送：{remaining}")
            return
        for gid in remaining:
            try:
                await client.send_group_msg(group_id=int(gid), message=text)
            except Exception as e:
                logger.warning(f"抽奖通知发送失败({gid}): {e}")
