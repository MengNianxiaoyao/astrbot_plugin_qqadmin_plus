from aiocqhttp import CQHttp
from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from ...config import PluginConfig
from ...data import QQAdminDB, QQAdminGlobalList
from ...utils import (
    extract_message_id,
    get_nickname,
    parse_cq_to_chain,
    resolve_allow_ids,
)
from .review import JoinReviewer
from .state import JoinState


class JoinEvents:
    """进群事件监听：入群申请审核、退群、欢迎与禁言。"""

    def __init__(
        self,
        config: PluginConfig,
        db: QQAdminDB,
        global_list: QQAdminGlobalList,
        group_cache,
        state: JoinState,
        reviewer: JoinReviewer,
    ):
        self.cfg = config
        self.db = db
        self.global_list = global_list
        self._group_cache = group_cache
        self.state = state
        self.reviewer = reviewer

    async def _get_group_name(self, gid: str) -> str:
        if self._group_cache:
            try:
                group = await self._group_cache.get_group(gid)
                name = group.get("group_name", "")
                if name:
                    return name
            except Exception:
                pass
        return gid

    async def _send_admin(self, client: CQHttp, message: str) -> list[str]:
        """向bot管理员私聊发送消息，返回已发送消息的消息ID列表。"""
        sent_ids: list[str] = []
        for admin_id in self.cfg.admins_id:
            try:
                result = await client.send_private_msg(user_id=int(admin_id), message=message)
                mid = extract_message_id(result)
                if mid:
                    sent_ids.append(mid)
            except Exception as e:
                logger.error(f"无法发送消息给bot管理员：{e}")
        return sent_ids

    async def event_monitoring(self, event: AiocqhttpMessageEvent):
        """监听进群/退群事件"""
        raw = getattr(event.message_obj, "raw_message", None)
        if not isinstance(raw, dict):
            logger.debug(f"event_monitoring 忽略非 dict raw_message: {type(raw)}")
            return

        gid: str = str(raw.get("group_id", ""))
        client = event.bot
        uid: str = str(raw.get("user_id", ""))

        # 进群申请事件
        if raw.get("post_type") == "request" and raw.get("request_type") == "group" and raw.get("sub_type") == "add":
            # 进群审核总开关
            if not await self.db.get(gid, "join_switch"):
                return
            comment = raw.get("comment")
            flag = raw.get("flag", "")
            # 登记本次申请，用于多群同时申请判定
            self.state.register_application(uid, gid, flag, await self._get_group_name(gid))
            info = await client.get_stranger_info(user_id=int(uid)) or {}
            nickname = info.get("nickname") or "未知昵称"
            if info.get("isHideQQLevel"):
                level = None
            else:
                level = info.get("qqLevel") or info.get("level")

            # 判断是否通过（满群时直接拒绝，白名单也不放行）
            approve, reason, code = await self.reviewer.should_approve(gid, uid, comment, level, client=client)
            # 清理缓存
            if approve is True:
                self.state.fail.pop(f"{gid}_{uid}", None)
                self.state.fail_time.pop(f"{gid}_{uid}", None)

            # 自动审核
            if approve is not None:
                try:
                    await client.set_group_add_request(
                        flag=flag,
                        sub_type="add",
                        approve=approve,
                        reason="" if approve else reason,
                    )
                    # 自动落定后清除跨群申请记录（转人工的保留，继续阻拦其他群申请）
                    self.state.drop_application(uid, gid)
                    # 命中免通知类型时不发送通知（按结果代号判定，与展示文案解耦；自动批准/驳回均适用）
                    silent = await self.db.get(
                        gid,
                        "join_silent_reasons",
                        ["full", "allow", "white_word", "block", "empty_msg"],
                    )
                    silent = set(silent or [])
                    if code in silent:
                        return
                    approve_msg = f"自动{'批准' if approve else '驳回'}，{reason}"
                except Exception as e:
                    logger.warning(f"set_group_add_request failed: {e}")
                    return
            else:
                approve_msg = reason

            # 生成并发送通知
            group_name = await self._get_group_name(gid)
            tip = "批准/驳回" if approve is None else "自动审核"
            notice = f"【进群申请-{tip}】\n群：{group_name}\n昵称：{nickname}\nQQ：{uid}\nflag：{flag}\n等级：{level}"
            if comment:
                notice += f"\n{comment}"
            if approve_msg:
                notice += f"\n处理结果：{approve_msg}"

            group_config = self.db.get_group_snapshot(gid)
            sent_ids: list[str] = []
            if group_config.get("admin_audit", self.cfg.admin_audit):
                sent_ids = await self._send_admin(client, notice)
            else:
                result = await event.send(event.plain_result(notice))
                mid = extract_message_id(result)
                if mid:
                    sent_ids.append(mid)
            for mid in sent_ids:
                self.state.track_pending(mid, gid, uid, nickname, flag)

        # 主动退群事件
        elif raw.get("post_type") == "notice" and raw.get("notice_type") == "group_decrease" and raw.get("sub_type") == "leave":
            should_block = await self.db.get(gid, "leave_block", False)
            should_notify = await self.db.get(gid, "leave_notify", False)
            if should_block:
                allow_ids = await resolve_allow_ids(self.db, self.global_list, gid)
                if uid not in allow_ids:
                    await self.reviewer.add_to_block(gid, uid)
                    did_block = True
                else:
                    did_block = False
            else:
                did_block = False
            if should_notify:
                nickname = await get_nickname(event, uid)
                msg = f"{nickname}({uid}) 主动退群了"
                if did_block:
                    msg += "，已拉黑"
                await event.send(event.plain_result(msg))

        # 进群欢迎、禁言
        elif raw.get("notice_type") == "group_increase" and uid != event.get_self_id():
            # 进群欢迎
            join_welcome = await self.db.get(gid, "join_welcome")
            if join_welcome:
                nickname = await get_nickname(event, uid)
                # 兼容 {nickname}/{qq} 双变量;用 replace 避免 format 缺键抛异常
                welcome = str(join_welcome).replace("{nickname}", nickname).replace("{qq}", uid)
                if welcome:
                    try:
                        chain = parse_cq_to_chain(welcome, allowed_roots=[self.cfg.data_dir, self.cfg.plugin_dir])
                    except Exception as e:
                        logger.warning(f"解析进群欢迎词失败: {e}, 回退为纯文本")
                        chain = []
                    if chain:
                        try:
                            # 优先用 chain_result (当前主干通用写法)
                            await event.send(event.chain_result(chain))
                        except Exception as e:
                            logger.warning(f"发送欢迎词富文本失败: {e}, 回退为纯文本")
                            await event.send(event.plain_result(welcome))
                    else:
                        await event.send(event.plain_result(welcome))
            # 进群禁言
            join_ban_time = await self.db.get(gid, "join_ban_time", 0)
            if join_ban_time > 0:
                try:
                    await client.set_group_ban(
                        group_id=int(gid),
                        user_id=int(uid),
                        duration=join_ban_time,
                    )
                except Exception:
                    pass
