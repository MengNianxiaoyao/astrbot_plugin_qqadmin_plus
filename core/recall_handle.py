import asyncio

from astrbot.api import logger
from astrbot.core.message.components import At, Reply
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from ..config import PluginConfig
from ..data import QQAdminDB
from ..utils import get_ats


class RecallHandle:
    def __init__(self, config: PluginConfig, db: QQAdminDB):
        self.cfg = config
        self.db = db

    async def delete_msg(self, event: AiocqhttpMessageEvent):
        """(引用消息)撤回 | 撤回 @某人(默认bot) 数量(默认10)"""
        client = event.bot
        chain = event.get_messages()
        if not chain:
            await event.send(event.plain_result("未获取到可撤回的消息"))
            return
        reply_seg = next((seg for seg in chain if isinstance(seg, Reply)), None)
        if reply_seg:
            try:
                await client.delete_msg(message_id=int(reply_seg.id))
            except Exception:
                await event.send(event.plain_result("我无权撤回这条消息"))
            finally:
                event.stop_event()
            return
        elif any(isinstance(seg, At) for seg in chain):
            target_ids = get_ats(event) or [event.get_self_id()]
            target_ids = {str(uid) for uid in target_ids}

            parts = event.message_str.split()
            end_arg = parts[-1] if parts else ""
            count = int(end_arg) if end_arg.isdigit() else 10
            count = max(1, min(count, 50))

            payloads = {
                "group_id": int(event.get_group_id()),
                "message_seq": 0,
                "count": count,
                "reverseOrder": True,
            }
            try:
                result = await client.api.call_action("get_group_msg_history", **payloads)
            except Exception as e:
                logger.warning(f"获取群聊消息记录失败: {e}")
                await event.send(event.plain_result("获取群聊消息记录失败"))
                event.stop_event()
                return

            raw_messages = []
            if isinstance(result, dict):
                raw_messages = result.get("messages", []) or []
            messages = [m for m in raw_messages if isinstance(m, dict)]
            delete_count = 0
            sem = asyncio.Semaphore(10)

            # 撤回消息
            async def try_delete(message: dict):
                nonlocal delete_count
                try:
                    sender = message.get("sender", {}) or {}
                    if str(sender.get("user_id")) not in target_ids:
                        return
                    message_id = message.get("message_id")
                    if not message_id:
                        return
                    async with sem:
                        try:
                            await client.delete_msg(message_id=message_id)
                            delete_count += 1
                        except Exception:
                            pass
                except Exception:
                    pass

            # 并发撤回
            tasks = [try_delete(msg) for msg in messages]
            await asyncio.gather(*tasks)

            await event.send(event.plain_result(f"已从{count}条消息中撤回{delete_count}条"))
            event.stop_event()
        else:
            await event.send(event.plain_result("用法：引用要撤回的消息，或 撤回 @群友 [数量]"))
            event.stop_event()
