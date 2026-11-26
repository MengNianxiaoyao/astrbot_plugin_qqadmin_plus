"""抽奖门面：装配 store/engine/wizard，负责生命周期、参与与查询。

- 发起是 Bot 管理员私聊功能，问答在 .wizard；群聊调用由 main 的入口过滤器直接拒绝。
- 每群同一时间只允许一场抽奖；多群联合共用奖池，开奖通知发送到所有参与群。
- 持久化在 .lottery_store；开奖/发奖/广播在 .engine；文案渲染在 .render。
"""

import asyncio
import time

from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from ...config import PluginConfig, get_plugin_timezone
from ...utils import get_ats, get_group_member, safe_int
from .engine import LotteryEngine
from .lottery_store import LotteryStore
from .models import MAX_HISTORY, STORE_VERSION, Lottery, finished_reason
from .render import active_text, history_text, join_progress_text, result_lines, user_history_text
from .wizard import LotteryWizard


class LotteryHandle:
    def __init__(self, context, config: PluginConfig, group_cache=None):
        self.context = context
        self.group_cache = group_cache
        self.tz = get_plugin_timezone(context)
        self.store = LotteryStore(config.data_dir / "lottery_data.json")
        self.active: dict[str, Lottery] = {}
        self.last: dict[str, Lottery] = {}
        self.history: dict[str, list[Lottery]] = {}
        self.wizards: set[str] = set()
        self._init_lock = asyncio.Lock()
        self._restored = False
        self.engine = LotteryEngine(self)
        self.wizard = LotteryWizard(self)

    # ============================== 生命周期 / 持久化 ==============================

    async def initialize(self):
        """加载持久化数据并恢复活动抽奖（幂等）。"""
        async with self._init_lock:
            if self._restored:
                return
            raw_active, raw_history, raw_index = self.store.load()
            self._restore_history(raw_history, raw_index)
            self._restore_active(raw_active)
            self._restored = True
            await self.save(immediate=True)

    async def ensure_ready(self):
        if not self._restored:
            await self.initialize()

    async def stop(self):
        if self._restored:
            await self.save(immediate=True)
        await self.store.flush()
        self.engine.cancel_all()

    def snapshot(self) -> dict:
        return {
            "version": STORE_VERSION,
            "active": [LotteryStore.lottery_to_dict(lottery) for lottery in self._active_values()],
            # 场次表每场只写一次，每个参与群再各留一份场次 ID 索引
            "history": [LotteryStore.lottery_to_dict(item) for item in self._history_values()],
            "index": {str(gid): [item.id for item in records] for gid, records in self.history.items() if records},
        }

    async def save(self, immediate: bool = False):
        await self.store.save(self.snapshot, immediate)

    def _active_values(self) -> list[Lottery]:
        seen: set[int] = set()
        values: list[Lottery] = []
        for lottery in self.active.values():
            if id(lottery) in seen:
                continue
            seen.add(id(lottery))
            values.append(lottery)
        return values

    def _history_values(self) -> list[Lottery]:
        seen: set[str] = set()
        values: list[Lottery] = []
        for records in self.history.values():
            for item in records:
                if item.id in seen:
                    continue
                seen.add(item.id)
                values.append(item)
        return values

    def _restore_history(self, raw_history: list, raw_index: dict):
        by_id: dict[str, Lottery] = {}
        for record in raw_history:
            lottery = self.store.record_to_lottery(record)
            if lottery is not None:
                by_id[lottery.id] = lottery
        for gid, ids in raw_index.items():
            if not isinstance(ids, list):
                continue
            restored = [by_id[rid] for rid in ids if isinstance(rid, str) and rid in by_id][:MAX_HISTORY]
            if restored:
                self.history[str(gid)] = restored
                self.last[str(gid)] = restored[0]

    def _restore_active(self, raw_active: list):
        now = time.time()
        for record in raw_active:
            try:
                lottery = self.store.record_to_lottery(record)
            except Exception as e:
                logger.error(f"恢复抽奖记录失败: {e}")
                continue
            if lottery is None:
                continue
            if any(gid in self.active for gid in lottery.groups):
                logger.warning(f"抽奖恢复跳过：参与群已被占用 {lottery.groups}")
                continue
            for gid in lottery.groups:
                self.active[gid] = lottery
            reason = finished_reason(lottery, now)
            if reason:
                # 离线期间已满足结束条件：保留在 active 中标记 closing，避免开奖前被保存逻辑丢掉
                lottery.closing = True
                self.engine.spawn(self.engine.safe_draw(lottery, reason=reason))
            else:
                lottery.closing = False
                if lottery.expire_at:
                    self.engine.schedule(lottery)

    # ============================== 发起 / 参与 ==============================

    async def start_lottery(self, event: AiocqhttpMessageEvent):
        await self.wizard.start(event)

    async def join_lottery(self, event: AiocqhttpMessageEvent) -> list[str]:
        """返回需要回复到当前群的文本，由入口 yield 给 AstrBot（自动 @/引用）。"""
        await self.ensure_ready()
        group_id = event.get_group_id()
        lottery = self.active.get(group_id)
        if lottery is None:
            return ["本群当前没有进行中的抽奖"]
        if lottery.closed or lottery.closing:
            return ["本群抽奖正在开奖，请稍候"]

        user_id = str(event.get_sender_id())
        if user_id in lottery.participants:
            return ["你已参与本次抽奖，不能重复参与"]

        if lottery.min_level or lottery.titles:
            # 只有设了门槛才需要群资料；无门槛直接跳过接口查询
            member = await get_group_member(event.bot, group_id, user_id)
            reason = self._check_threshold(lottery, member, user_id)
            if reason:
                return [reason]
        # 昵称直接用消息自带的发送人名称，不再为昵称多查一次接口
        nickname = event.get_sender_name() or user_id
        # 等待群资料期间抽奖可能已开奖/被其他人抽满：进入临界区前再校验一次
        if lottery.closed or lottery.closing:
            return ["本群抽奖正在开奖，请稍候"]
        if user_id in lottery.participants:
            return ["你已参与本次抽奖，不能重复参与"]
        if (
            lottery.mode == "instant"
            and lottery.max_participants
            and len(lottery.participants) >= lottery.max_participants
        ):
            return ["参与人数已达上限，抽奖正在开奖"]
        lottery.participants[user_id] = nickname
        lottery.user_groups[user_id] = group_id

        if lottery.mode == "instant":
            texts = await self.engine.instant_draw(event, lottery, user_id, nickname)
        else:
            reason = finished_reason(lottery)
            if reason:
                texts = await self.engine.safe_draw(lottery, event=event, reason=reason)
            else:
                texts = [join_progress_text(lottery)]
        if not lottery.closed:
            await self.save()
        return texts

    def _check_threshold(self, lottery: Lottery, member: dict, user_id: str) -> str | None:
        if not lottery.min_level and not lottery.titles:
            return None
        # 按“用户实际参与的那个群”校验等级/头衔，多群联合抽奖时各群门槛独立生效
        if not member:
            logger.warning(f"抽奖校验群资料失败({user_id})")
            return "获取你的群资料失败，请稍后再试"

        level = safe_int(member.get("level"), 0)
        title = str(member.get("title") or "").strip()
        if lottery.min_level and lottery.titles:
            level_ok = level >= lottery.min_level
            title_ok = title in lottery.titles
            passed = (level_ok and title_ok) if lottery.threshold_all else (level_ok or title_ok)
            if passed:
                return None
            allowed = "/".join(f"「{item}」" for item in lottery.titles)
            if lottery.threshold_all:
                return (
                    f"参与门槛：群等级需不低于 {lottery.min_level} 级"
                    f"且群头衔需为 {allowed} 之一（你当前 {level} 级、头衔：{title or '无'}）"
                )
            return (
                f"参与门槛：群等级需不低于 {lottery.min_level} 级"
                f"或群头衔为 {allowed} 之一（你当前 {level} 级、头衔：{title or '无'}）"
            )
        if lottery.min_level and level < lottery.min_level:
            return f"参与门槛：群等级需不低于 {lottery.min_level} 级（你当前 {level} 级）"
        if lottery.titles and title not in lottery.titles:
            allowed = "/".join(f"「{item}」" for item in lottery.titles)
            return f"参与门槛：群头衔需为 {allowed} 之一（你当前：{title or '无'}）"
        return None

    # ============================== 查询 ==============================

    async def draw_now(self, event: AiocqhttpMessageEvent) -> list[str]:
        """立即结束当前群的抽奖并开奖（多群联合时整场开奖）。"""
        await self.ensure_ready()
        lottery = self.active.get(event.get_group_id())
        if lottery is None:
            return ["本群当前没有进行中的抽奖"]
        if lottery.closed:
            return ["本群抽奖已开奖或已取消"]
        if lottery.closing:
            return ["本群抽奖正在开奖，请稍候"]
        return await self.engine.safe_draw(lottery, event=event, reason="立即开奖")

    async def cancel_lottery(self, event: AiocqhttpMessageEvent) -> list[str]:
        """取消当前群的抽奖：不发奖、不留往期记录。"""
        await self.ensure_ready()
        lottery = self.active.get(event.get_group_id())
        if lottery is None:
            return ["本群当前没有进行中的抽奖"]
        text = f"抽奖「{lottery.prize}」已取消，本次不发放奖品"
        await self.engine.cancel_draw(lottery)
        # 其他参与群主动通知；当前群由入口 yield
        await self.engine.broadcast(lottery, text, skip={event.get_group_id()})
        return [text]

    async def claim(self, event: AiocqhttpMessageEvent) -> list[str]:
        """私聊领取预存奖品；群聊调用只提示私聊。"""
        if not event.is_private_chat():
            return ["请私聊机器人发送「领奖」领取预存奖品"]
        await self.ensure_ready()
        user_id = str(event.get_sender_id())
        texts: list[str] = []
        claimed = False
        for lottery in [*self._active_values(), *self._history_values()]:
            item = lottery.pending_items.pop(user_id, None)
            if not item:
                continue
            texts.append(f"【{lottery.prize}】\n{item}")
            claimed = True
        if not texts:
            return ["没有待领取的奖品"]
        if claimed:
            await self.save(immediate=True)
        return texts

    async def query_lottery(
        self, event: AiocqhttpMessageEvent, keyword: str = "", index: int | None = None
    ) -> list[str]:
        await self.ensure_ready()
        group_id = event.get_group_id()
        targets = get_ats(event)
        keyword = str(keyword or "").strip()
        history = self.history.get(group_id, [])

        if keyword in {"往期", "往期抽奖", "往期记录", "历史", "记录"} or (keyword.isdigit() and not targets):
            if targets:
                return [user_history_text(history, targets[0], self.tz)]
            if index is None and keyword.isdigit():
                index = int(keyword)
            if index is None:
                return [history_text(history, self.tz)]
            return await self._history_detail(history, index)

        lottery = self.active.get(group_id)
        if lottery is not None:
            text = active_text(lottery)
            if targets:
                user_id = targets[0]
                if user_id in lottery.participants:
                    text += f"\n{lottery.participants[user_id]}（{user_id}）已参与"
                else:
                    text += f"\n用户 {user_id} 尚未参与"
            return [text]

        lottery = self.last.get(group_id)
        if lottery is None:
            return ["本群暂无抽奖记录"]
        if targets:
            user_id = targets[0]
            winner = next((name for uid, name in lottery.winners if uid == user_id), None)
            if winner:
                return [f"{winner}（{user_id}）在上次抽奖「{lottery.prize}」中中奖"]
            return [f"用户 {user_id} 未在上次抽奖「{lottery.prize}」中中奖"]
        return ["\n".join(result_lines(lottery, self.tz, "【上次抽奖结果】"))]

    async def _history_detail(self, history: list[Lottery], index) -> list[str]:
        if not history:
            return ["本群暂无往期抽奖记录"]
        if not isinstance(index, int):
            if str(index).isdigit():
                index = int(index)
            else:
                return ["往期序号需为数字，例如「/抽奖查询 往期 1」"]
        if not 1 <= index <= len(history):
            return [f"往期序号需在 1~{len(history)} 之间"]
        return ["\n".join(result_lines(history[index - 1], self.tz, f"【往期抽奖 #{index}】"))]
