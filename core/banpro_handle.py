import asyncio
import json
import time
from collections import defaultdict, deque

from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from ..config import PluginConfig
from ..data import QQAdminDB
from ..utils import get_ats, get_nickname, parse_bool


class BanproHandle:
    def __init__(self, config: PluginConfig, db: QQAdminDB):
        self.cfg = config
        self.db = db
        self.builtin_ban_data = json.loads(config.ban_lexicon_path.read_text(encoding="utf-8"))
        self.builtin_ban_version = str(self.builtin_ban_data.get("lastUpdateDate", "未知"))
        self.builtin_ban_words = self._clean_words(self.builtin_ban_data.get("words", []))
        self.global_ban_words = self._load_global_ban_words()
        # 不用 maxlen 固定，动态读取 cfg.spamming_count，便于热更新与手动裁剪
        self.msg_timestamps: dict[str, dict[str, deque[float]]] = defaultdict(lambda: defaultdict(deque))
        self.last_banned_time: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        # 记录投票 {group_id: {"target": target_id, "votes": {user_id: bool}, "expire": timestamp, "threshold": threshold,}}
        self.vote_cache: dict[str, dict] = {}

    @staticmethod
    def _clean_words(words) -> list[str]:
        return list(dict.fromkeys(str(word).strip() for word in words if str(word).strip()))

    def _load_global_ban_words(self) -> list[str]:
        path = self.cfg.global_ban_lexicon_path
        if not path.exists():
            self._save_global_ban_words(self.builtin_ban_words)
            return list(self.builtin_ban_words)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return self._clean_words(data.get("words", []) if isinstance(data, dict) else data)
        except Exception as e:
            logger.warning(f"读取全局禁词失败，已回退内置禁词: {e}")
            return list(self.builtin_ban_words)

    def _save_global_ban_words(self, words: list[str]) -> None:
        self.global_ban_words = self._clean_words(words)
        self.cfg.global_ban_lexicon_path.write_text(
            json.dumps({"words": self.global_ban_words}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get_global_ban_words(self) -> list[str]:
        return list(self.global_ban_words)

    def get_available_builtin_ban_words(self) -> list[str]:
        global_words = set(self.global_ban_words)
        return [word for word in self.builtin_ban_words if word not in global_words]

    def set_global_ban_words(self, words: list[str]) -> list[str]:
        self._save_global_ban_words(words)
        return self.get_global_ban_words()

    def import_builtin_ban_words(self, words: list[str]) -> list[str]:
        builtin_words = set(self.builtin_ban_words)
        return self.set_global_ban_words(self.global_ban_words + [word for word in words if word in builtin_words])

    def restore_builtin_ban_words(self) -> list[str]:
        return self.set_global_ban_words(self.builtin_ban_words)

    async def handle_word_ban_time(self, event: AiocqhttpMessageEvent, time: int | None):
        """设置禁词禁言时长"""
        gid = event.get_group_id()
        if isinstance(time, int):
            await self.db.set(gid, "word_ban_time", time)
            msg = f"本群禁词禁言时长已设为：{time} 秒" if time > 0 else "本群禁词禁言已关闭"
            await event.send(event.plain_result(msg))
        else:
            status = await self.db.get(gid, "word_ban_time", 0)
            await event.send(event.plain_result(f"本群禁词禁言时长：{status} 秒"))

    async def handle_ban_words(self, event: AiocqhttpMessageEvent):
        """设置/查看违禁词"""
        gid = event.get_group_id()
        raw = event.message_str.partition(" ")[2]

        # 1. 空指令：查看
        if not raw:
            words = await self.db.get(gid, "custom_ban_words", [])
            await event.send(event.plain_result(f"本群违禁词：{words}"))
            return

        # 2. 纯单词列表（无 +/-）：整表覆写
        toks = raw.split()
        if all(not tok.startswith(("+", "-")) for tok in toks):
            await self.db.set(gid, "custom_ban_words", toks)
            await event.send(event.plain_result(f"本群违禁词已覆写为：{' '.join(toks)}"))
            return

        # 3. 增量模式：+word / -word
        curr = set(await self.db.get(gid, "custom_ban_words", []))
        added, removed = [], []

        for tok in toks:
            if tok.startswith("+") and len(tok) > 1:
                w = tok[1:]
                if w not in curr:
                    curr.add(w)
                    added.append(w)
            elif tok.startswith("-") and len(tok) > 1:
                w = tok[1:]
                if w in curr:
                    curr.discard(w)
                    removed.append(w)

        await self.db.set(gid, "custom_ban_words", list(curr))

        reply = ["本群违禁词"]
        if added:
            reply.append(f"新增：{'、'.join(added)}")
        if removed:
            reply.append(f"移除：{'、'.join(removed)}")
        if not added and not removed:
            reply.append("无变动")
        await event.send(event.plain_result("\n".join(reply)))

    async def handle_builtin_ban_words(self, event: AiocqhttpMessageEvent, mode_str: str | bool | None):
        """启用/停用全局违禁词"""
        gid = event.get_group_id()
        mode = parse_bool(mode_str)

        if isinstance(mode, bool):
            await self.db.set(gid, "builtin_ban", mode)
            await event.send(event.plain_result(f"本群全局禁词：{mode}"))
        else:
            status = await self.db.get(gid, "builtin_ban", False)
            await event.send(event.plain_result(f"本群全局禁词：{status}"))

    async def on_ban_words(self, event: AiocqhttpMessageEvent):
        """检测禁词并撤回消息、禁言用户"""
        gid = event.get_group_id()
        snapshot = self.db.get_group_snapshot(gid)

        # 检测自定义的违禁词
        if ban_words := snapshot.get("custom_ban_words", []):
            if await self.check_ban_words(event, ban_words):
                return

        # 检测内置违禁词
        if snapshot.get("builtin_ban", False):
            if await self.check_ban_words(event, self.global_ban_words):
                return

    async def check_ban_words(self, event: AiocqhttpMessageEvent, ban_words: list[str]) -> bool:
        """检测违禁词并撤回消息；忽略空词与单字词以避免过度匹配"""
        gid = event.get_group_id()
        msg = event.message_str.lower()
        for word in ban_words:
            w = str(word).strip()
            if not w:
                continue
            if len(w) == 1:
                logger.warning(f"跳过单字禁词以避免过度匹配: {w!r} (群{gid})")
                continue
            if w.lower() in msg:
                # 撤回消息
                try:
                    message_id = event.message_obj.message_id
                    await event.bot.delete_msg(message_id=int(message_id))
                except Exception:
                    pass
                # 禁言发送者
                ban_time = await self.db.get(gid, "word_ban_time", 0)
                if ban_time > 0:
                    try:
                        await event.bot.set_group_ban(
                            group_id=int(event.get_group_id()),
                            user_id=int(event.get_sender_id()),
                            duration=ban_time,
                        )
                    except Exception:
                        logger.error(f"bot在群{event.get_group_id()}权限不足，禁言失败")
                        pass
                return True
        return False

    async def handle_spamming_ban_time(self, event: AiocqhttpMessageEvent, time: int | None):
        """设置刷屏禁言时长"""
        gid = event.get_group_id()
        if isinstance(time, int):
            await self.db.set(gid, "spamming_ban_time", time)
            msg = f"本群刷屏禁言时长已设为：{time} 秒" if time > 0 else "本群刷屏禁言已关闭"
            await event.send(event.plain_result(msg))
        else:
            status = await self.db.get(gid, "spamming_ban_time", 0)
            await event.send(event.plain_result(f"本群刷屏禁言时长：{status} 秒"))

    async def spamming_ban(self, event: AiocqhttpMessageEvent):
        """刷屏禁言"""
        group_id = event.get_group_id()
        sender_id = event.get_sender_id()
        snapshot = self.db.get_group_snapshot(group_id)
        ban_time = snapshot.get("spamming_ban_time", 0)
        if sender_id == event.get_self_id() or ban_time <= 0 or len(event.get_messages()) == 0:
            return

        now = time.time()

        last_time = self.last_banned_time[group_id][sender_id]
        if now - last_time < ban_time:
            return

        timestamps = self.msg_timestamps[group_id][sender_id]
        timestamps.append(now)
        count = int(self.cfg.spamming_count)
        interval_thr = float(self.cfg.spamming_interval)
        # 手动按 count 裁剪，支持配置热更新
        while len(timestamps) > count:
            timestamps.popleft()
        if len(timestamps) >= count:
            recent = list(timestamps)[-count:]
            intervals = [recent[i + 1] - recent[i] for i in range(count - 1)]
            if all(interval < interval_thr for interval in intervals):
                # 提前写入禁止标记，防止并发重复禁
                self.last_banned_time[group_id][sender_id] = now

                try:
                    await event.bot.set_group_ban(
                        group_id=int(group_id),
                        user_id=int(sender_id),
                        duration=ban_time,
                    )
                    nickname = await get_nickname(event, sender_id)
                    await event.send(event.plain_result(f"检测到{nickname}刷屏，已禁言"))
                except Exception:
                    logger.error(f"bot在群{group_id}权限不足，禁言失败")
                timestamps.clear()
        # 惰性清理：长时间未刷屏的用户释放内存
        if len(timestamps) == 1 and (now - timestamps[0] > max(ban_time, 3600)):
            self.msg_timestamps[group_id].pop(sender_id, None)
            self.last_banned_time[group_id].pop(sender_id, None)
            if not self.msg_timestamps[group_id]:
                self.msg_timestamps.pop(group_id, None)
                self.last_banned_time.pop(group_id, None)

    async def start_vote_mute(self, event, ban_time: int | None = None):
        """
        发起投票禁言：如果已有对该用户的投票，直接提示
        """
        target_ids = get_ats(event)
        if not target_ids:
            return
        target_id = target_ids[0]
        group_id = event.get_group_id()
        group_config = self.db.get_group_snapshot(group_id)
        ban_time = self.cfg.get_ban_time_with_range(group_config.get("random_ban_time"), ban_time)

        if group_id in self.vote_cache:
            record = self.vote_cache[group_id]
            if record.get("target") == target_id:
                await event.send(event.plain_result("已存在对该用户的禁言投票"))
            else:
                nickname0 = await get_nickname(event, record["target"])
                await event.send(event.plain_result(f"群内已有对 {nickname0} 正在进行的禁言投票"))
            return

        vote_ban = group_config.get("vote_ban", {})
        ttl = int(vote_ban.get("ttl", self.cfg.vote_ban.ttl))
        threshold = int(vote_ban.get("threshold", self.cfg.vote_ban.threshold))

        expire_at = time.time() + ttl
        record = {
            "target": target_id,
            "votes": {},
            "ban_time": ban_time,
            "expire": expire_at,
            "threshold": threshold,
        }
        self.vote_cache[group_id] = record

        nickname = await get_nickname(event, target_id)
        await event.send(event.plain_result(f"已发起对 {nickname} 的禁言投票(禁言{ban_time}秒)，输入“赞同禁言/反对禁言”进行表态，{ttl}秒后结算"))

        # ===== 新增：定时结算逻辑 =====
        async def settle_vote():
            await asyncio.sleep(ttl)
            current = self.vote_cache.get(group_id)
            if current is not record:
                return  # 已被提前结算
            votes = list(record["votes"].values())
            agree_count = sum(votes)
            disagree_count = len(votes) - agree_count
            nickname2 = await get_nickname(event, record["target"])

            # 到期按多数票决定（平票视为否决）
            if agree_count > disagree_count:
                try:
                    await event.bot.set_group_ban(
                        group_id=int(group_id),
                        user_id=int(record["target"]),
                        duration=record["ban_time"],
                    )
                    await event.send(event.plain_result(f"投票时间到！已禁言{nickname2}"))
                except Exception:
                    logger.error(f"bot在群{group_id}权限不足，禁言失败")
            else:
                await event.send(event.plain_result(f"投票时间到！禁言被否决，{nickname2}安全了"))
            # 清理投票记录
            self.vote_cache.pop(group_id, None)

        asyncio.create_task(settle_vote())

    async def vote_mute(self, event: AiocqhttpMessageEvent, agree: bool):
        """
        赞同/反对禁言
        agree=True 表示赞同，False 表示反对
        """
        group_id = event.get_group_id()
        voter_id = event.get_sender_id()

        record = self.vote_cache.get(group_id)
        if not record:
            await event.send(event.plain_result("当前没有进行中的禁言投票"))
            return

        threshold = record["threshold"]
        target_id = record["target"]

        # 记录/更新该用户的立场
        record["votes"][voter_id] = agree

        votes = list(record["votes"].values())
        agree_count = sum(votes)
        disagree_count = len(votes) - agree_count
        nickname = await get_nickname(event, target_id)

        # 提前达成赞同阈值 → 立即禁言
        if agree_count >= threshold:
            try:
                await event.bot.set_group_ban(
                    group_id=int(group_id),
                    user_id=int(target_id),
                    duration=record["ban_time"],
                )
                await event.send(event.plain_result(f"投票通过！已禁言{nickname}"))
            except Exception:
                logger.error(f"bot在群{group_id}权限不足，禁言失败")
            finally:
                # 清理记录（定时任务见前面会检测到记录已删除并直接返回）
                self.vote_cache.pop(group_id, None)
            return

        # 移除“反对阈值提前否决”，仅保留赞同阈值提前通过；否则等待 TTL 多数决，避免少数反对劫持
        # 否则展示当前进度
        await event.send(event.plain_result(f"禁言【{nickname}】：\n赞同({agree_count}/{threshold})\n反对({disagree_count}/{threshold})"))
