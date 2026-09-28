from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from ...utils import get_reply_message_str, get_replyer_message_id
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
        if not text:
            return "未引用任何【进群申请】"

        nickname = None
        flag = None
        pending_entry = None

        # A2：按被引用消息ID精确匹配已登记的通知
        reply_id = get_replyer_message_id(event)
        if reply_id:
            pending_entry = self.state.pending.pop(reply_id, None)
            if pending_entry:
                flag = pending_entry["flag"]
                nickname = pending_entry["nickname"]

        # A1：兜底从通知文本中按内容定位解析（不依赖固定行号）
        if flag is None:
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
            # 人工落定后清除跨群申请记录
            self.state.drop_application_by_flag(flag)
            if approve:
                reply = f"已同意{nickname}进群"
            else:
                reply = f"已拒绝{nickname}进群" + (f"\n理由：{extra}" if extra else "")
            return reply
        except Exception as e:
            logger.error(f"处理进群申请失败: {e}")
            if reply_id and pending_entry is not None:
                # 失败可重试：恢复已取出的登记记录
                self.state.pending[reply_id] = pending_entry
            return self._approve_fail_hint(e)

    async def agree_add_group(self, event: AiocqhttpMessageEvent, extra: str = ""):
        """批准进群申请"""
        reply = await self.set_approve(event=event, extra=extra, approve=True)
        if reply:
            await event.send(event.plain_result(reply))

    async def refuse_add_group(self, event: AiocqhttpMessageEvent, extra: str = ""):
        """驳回进群申请"""
        reply = await self.set_approve(event=event, extra=extra, approve=False)
        if reply:
            await event.send(event.plain_result(reply))
