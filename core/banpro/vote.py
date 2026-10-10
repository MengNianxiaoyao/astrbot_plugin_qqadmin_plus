"""投票模块：一群一票的状态机、阈值提前通过与 TTL 多数决结算。"""

import asyncio
import time

from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from ...config import PluginConfig
from ...data import QQAdminDB
from ...permission.levels import PermLevel
from ...permission.manager import perm_manager
from ...utils import get_ats, get_nickname


class VoteSession:
    """投票模块：一群一票的状态机、阈值提前通过与 TTL 多数决结算。"""

    def __init__(self, config: PluginConfig, db: QQAdminDB):
        self.cfg = config
        self.db = db
        # 记录投票 {group_id: {"target": target_id, "starter": starter_id, "votes": {user_id: bool}, "expire": timestamp, "threshold": threshold,}}
        self.vote_cache: dict[str, dict] = {}

    @staticmethod
    def _vote_label(kind: str) -> tuple[str, str]:
        """(投票名, 动作名)：mute→禁言投票/禁言，kick→踢出投票/踢出。"""
        if kind == "kick":
            return "踢出投票", "踢出"
        return "禁言投票", "禁言"

    @staticmethod
    def _tally(record: dict) -> tuple[int, int]:
        """计数唯一出口：返回 (赞同数, 反对数)。"""
        votes = list(record["votes"].values())
        agree_count = sum(votes)
        return agree_count, len(votes) - agree_count

    async def _punish(self, bot_client, gid_int: int, record: dict):
        """执行投票通过的处罚（禁言/踢出）；失败抛异常由调用方处理。"""
        if record.get("kind") == "kick":
            await bot_client.set_group_kick(
                group_id=gid_int,
                user_id=int(record["target"]),
                reject_add_request=False,
            )
        else:
            await bot_client.set_group_ban(
                group_id=gid_int,
                user_id=int(record["target"]),
                duration=record["ban_time"],
            )

    async def _settle_vote(self, group_id: str, gid_int: int, bot_client, event, record: dict, ttl: int):
        """TTL 到期按多数票结算（平票视为否决）；仅清理自己那条记录。"""
        await asyncio.sleep(ttl)
        current = self.vote_cache.get(group_id)
        if current is not record:
            return  # 已被提前结算或取消
        agree_count, disagree_count = self._tally(record)
        _, action = self._vote_label(record.get("kind", "mute"))
        try:
            nickname2 = await get_nickname(event, record["target"])
        except Exception:
            nickname2 = str(record["target"])

        # 到期按多数票决定（平票视为否决）
        try:
            if agree_count > disagree_count:
                try:
                    await self._punish(bot_client, gid_int, record)
                except Exception:
                    logger.error(f"bot在群{group_id}权限不足，{action}失败")
                    await bot_client.send_group_msg(group_id=gid_int, message=f"投票通过，但{action}{nickname2}失败（权限不足）")
                else:
                    await bot_client.send_group_msg(group_id=gid_int, message=f"投票时间到！已{action}{nickname2}")
            else:
                await bot_client.send_group_msg(group_id=gid_int, message=f"投票时间到！{action}被否决，{nickname2}安全了")
        except Exception as e:
            logger.warning(f"群{group_id}投票结算通知发送失败: {e}")
        finally:
            # 仅清理自己那条记录，避免误清后来的新投票
            if self.vote_cache.get(group_id) is record:
                self.vote_cache.pop(group_id, None)

    async def start_vote_mute(self, event, ban_time: int | None = None):
        """发起投票禁言：一群同时只能有一场投票"""
        await self._start_vote(event, "mute", ban_time)

    async def start_vote_kick(self, event):
        """发起投票踢人：一群同时只能有一场投票"""
        await self._start_vote(event, "kick")

    async def _start_vote(self, event, kind: str, ban_time: int | None = None):
        vote_name, _ = self._vote_label(kind)
        target_ids = get_ats(event)
        if not target_ids:
            return
        target_id = target_ids[0]
        group_id = event.get_group_id()
        group_config = self.db.get_group_snapshot(group_id)
        if kind == "mute":
            ban_time = self.cfg.get_ban_time_with_range(group_config.get("random_ban_time"), ban_time)
        # 结算不依赖发起时的 event 会话：预捕获 client 与群号，TTL 后直发群消息
        bot_client = event.bot
        gid_int = int(group_id)

        if group_id in self.vote_cache:
            record = self.vote_cache[group_id]
            existing_name, _ = self._vote_label(record.get("kind", "mute"))
            if record.get("target") == target_id:
                await event.send(event.plain_result(f"已存在对该用户的{existing_name}，可发送「取消投票」结束当前投票"))
            else:
                nickname0 = await get_nickname(event, record["target"])
                await event.send(event.plain_result(f"群内已有对 {nickname0} 正在进行的{existing_name}，可发送「取消投票」结束当前投票"))
            return

        vote_ban = group_config.get("vote_ban", {})
        ttl = int(vote_ban.get("ttl", self.cfg.vote_ban.ttl))
        threshold = int(vote_ban.get("threshold", self.cfg.vote_ban.threshold))

        expire_at = time.time() + ttl
        record = {
            "kind": kind,
            "target": target_id,
            "starter": str(event.get_sender_id()),
            "votes": {},
            "ban_time": ban_time if kind == "mute" else 0,
            "expire": expire_at,
            "threshold": threshold,
        }
        self.vote_cache[group_id] = record

        nickname = await get_nickname(event, target_id)
        if kind == "kick":
            start_msg = f"已发起对 {nickname} 的踢出投票，输入“【/赞同踢人】或【/反对踢人】”进行表态\n赞同满{threshold}票直接通过，{ttl}秒后结算（仅发起人/管理员可取消投票）"
        else:
            start_msg = f"已发起对 {nickname} 的{vote_name}(禁言{ban_time}秒)，输入“【/赞同禁言】或【/反对禁言】”进行表态\n赞同满{threshold}票直接通过，{ttl}秒后结算（仅发起人/管理员可取消投票）"
        await event.send(event.plain_result(start_msg))

        asyncio.create_task(self._settle_vote(group_id, gid_int, bot_client, event, record, ttl))

    async def cancel_vote_mute(self, event: AiocqhttpMessageEvent):
        """取消当前群正在进行的投票（仅发起人/管理员；权限由装饰器校验）；结算任务醒来后见记录已消失会自动退出"""
        group_id = event.get_group_id()
        record = self.vote_cache.get(group_id)
        if not record:
            await event.send(event.plain_result("当前没有进行中的投票"))
            return
        sender_id = str(event.get_sender_id())
        if sender_id != str(record.get("starter")) and await perm_manager.get_perm_level(event, sender_id) > PermLevel.ADMIN:
            await event.send(event.plain_result("仅投票发起人或管理员可取消投票"))
            return
        self.vote_cache.pop(group_id, None)
        vote_name, _ = self._vote_label(record.get("kind", "mute"))
        try:
            nickname = await get_nickname(event, record["target"])
        except Exception:
            nickname = str(record["target"])
        await event.send(event.plain_result(f"已取消对 {nickname} 的{vote_name}"))

    async def vote_mute(self, event: AiocqhttpMessageEvent, agree: bool):
        """
        赞同/反对当前投票（一群同时只有一场）
        agree=True 表示赞同，False 表示反对
        """
        group_id = event.get_group_id()
        voter_id = event.get_sender_id()

        record = self.vote_cache.get(group_id)
        if not record:
            await event.send(event.plain_result("当前没有进行中的投票"))
            return

        threshold = record["threshold"]
        target_id = record["target"]
        if str(voter_id) == str(target_id):
            await event.send(event.plain_result("被投票者不能参与表态"))
            return
        _, action = self._vote_label(record.get("kind", "mute"))

        # 记录/更新该用户的立场
        record["votes"][voter_id] = agree

        agree_count, disagree_count = self._tally(record)
        nickname = await get_nickname(event, target_id)

        # 提前达成赞同阈值 → 立即执行
        if agree_count >= threshold:
            try:
                await self._punish(event.bot, int(group_id), record)
                await event.send(event.plain_result(f"投票通过！已{action}{nickname}"))
            except Exception:
                logger.error(f"bot在群{group_id}权限不足，{action}失败")
            finally:
                # 清理记录（定时任务见前面会检测到记录已删除并直接返回）
                self.vote_cache.pop(group_id, None)
            return

        # 移除“反对阈值提前否决”，仅保留赞同阈值提前通过；否则等待 TTL 多数决，避免少数反对劫持
        # 否则展示当前进度
        await event.send(event.plain_result(f"{action}【{nickname}】：\n赞同({agree_count}/{threshold})\n反对({disagree_count}/{threshold})"))
