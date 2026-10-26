from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from ...utils import get_reply_message_str, get_reply_message_id
from .state import JoinState


class JoinApprover:
    """人工审批进群申请：引用通知精确匹配 + 文本兜底。"""

    def __init__(self, state: JoinState):
        self.state = state

    @staticmethod
    def _extract_field(text: str, key: str) -> str | None:
        """按“键前缀”定位字段内容，兼容通知行序变化（不依赖固定行号）。"""
        for line in text.splitlines():
            if line.startswith(key):
                value = line[len(key) :].strip()
                if value:
                    return value
        return None

    @staticmethod
    def _approve_fail_hint(exc: Exception) -> str:
        """依据进群审批 API 报错文案给出针对性提示。"""
        msg = str(exc).lower()
        if "already agree" in msg:
            return "这条申请已经被同意了"
        if "already refuse" in msg or "already disagree" in msg:
            return "这条申请已经被驳回了"
        return "处理失败，该申请可能已失效或已被处理，请核对后重试"

    async def set_approve(self, event: AiocqhttpMessageEvent, extra: str = "", approve: bool = True) -> str | None:
        """处理进群申请：优先按引用消息ID精确匹配，失败则按文本内容定位兜底。"""
        text = get_reply_message_str(event)
        reply_id = get_reply_message_id(event)
        if not text and not reply_id:
            return "未引用任何【进群申请】"

        nickname = None
        flag = None
        pending_entry = None

        # A2：按被引用消息ID精确匹配已登记的通知（只查看不取出，落定成功后按 flag 统一清）
        if reply_id:
            pending_entry = self.state.pending.get(reply_id)
            if pending_entry:
                flag = pending_entry["flag"]
                nickname = pending_entry["nickname"]

        # A1：兜底从通知文本中按内容定位解析（不依赖固定行号）；拿不到文本时只靠 A2
        if flag is None and text:
            try:
                nickname = self._extract_field(text, "昵称：") or nickname
                flag = self._extract_field(text, "flag：") or flag
            except Exception as e:
                logger.error(f"解析进群申请引用失败: {e}")
                return "这条申请格式不对"

        if not flag:
            return "引用的可能不是【进群申请】或该申请已失效"

        nickname = nickname or "该用户"
        try:
            await event.bot.set_group_add_request(flag=flag, sub_type="add", approve=approve, reason=extra)
            # 人工落定后清除跨群申请记录与待审批登记（引用/兜底路径统一按 flag 清）
            self.state.drop_application_by_flag(flag)
            self.state.drop_pending(lambda k, r: r.get("flag") == flag)
            if approve:
                reply = f"已同意{nickname}进群"
            else:
                reply = f"已拒绝{nickname}进群" + (f"\n理由：{extra}" if extra else "")
            return reply
        except Exception as e:
            logger.error(f"处理进群申请失败: {e}")
            hint = self._approve_fail_hint(e)
            err = str(e).lower()
            if "already agree" in err or "already refuse" in err or "already disagree" in err:
                # 站外已处理：登记已无意义，按 flag 对账，避免待审核计数占位
                self.state.drop_pending(lambda k, r: r.get("flag") == flag)
            return hint

    async def _send_reply(self, event: AiocqhttpMessageEvent, reply: str):
        """发送审批结果；发送失败只记日志，避免异常逃逸触发 AstrBot 兜底报错链"""
        try:
            await event.send(event.plain_result(reply))
        except Exception as e:
            logger.error(f"发送进群审批结果失败: {e}")

    async def agree_add_group(self, event: AiocqhttpMessageEvent, extra: str = ""):
        """批准进群申请"""
        reply = await self.set_approve(event=event, extra=extra, approve=True)
        if reply:
            await self._send_reply(event, reply)

    async def refuse_add_group(self, event: AiocqhttpMessageEvent, extra: str = ""):
        """驳回进群申请"""
        reply = await self.set_approve(event=event, extra=extra, approve=False)
        if reply:
            await self._send_reply(event, reply)
