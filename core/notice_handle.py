"""群公告：发布（纯文本/带图）与查看。图片先下载到本地再传 OneBot 接口。"""

import asyncio
import textwrap
import time

from astrbot.api import logger
from astrbot.api.message_components import Image
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.utils.datetime_utils import generate_timestamp_id
from astrbot.core.utils.quoted_message_parser import extract_quoted_message_images

from ..config import PluginConfig
from ..utils import download_file, format_time, unwrap_data


class NoticeHandle:
    def __init__(self, config: PluginConfig):
        self.cfg = config

    async def send_group_notice(
        self,
        event: AiocqhttpMessageEvent,
        content: str = "",
        image_url: str | None = None,
    ):
        """(引用图片)发布群公告 xxx"""
        if not content:
            parts = event.get_message_str().split(maxsplit=1)
            content = parts[1].strip() if len(parts) > 1 else ""
        if not content:
            return "未指定群公告内容"

        gid = event.get_group_id()
        image_path = ""
        if not image_url:
            refs = await extract_quoted_message_images(event)
            if not refs:
                refs = [seg.url for seg in event.get_messages() if isinstance(seg, Image) and seg.url]
            image_url = next((ref for ref in refs if ref.startswith(("http://", "https://"))), None)
        if image_url:
            img_name = f"{gid}_{generate_timestamp_id()}.png"
            temp_path = self.cfg.group_notice_dir / img_name

            logger.debug(f"Group notice image temp path: {temp_path}")
            image_path = await download_file(image_url, temp_path)
            if not image_path:
                return "图片获取失败"

        payload = {
            "group_id": int(gid),
            "content": content,
        }
        if image_path:
            payload["image"] = str(image_path)

        last_error: Exception | None = None
        # go-cqhttp / NapCat 用带下划线的扩展接口名；部分实现只提供不带下划线的版本
        for action in ("_send_group_notice", "send_group_notice"):
            try:
                await event.bot.call_action(action, **payload)
                last_error = None
                break
            except Exception as e:
                last_error = e
        if last_error is not None:
            logger.warning(f"发布群公告失败: {last_error}")
            return f"群公告发布失败：{last_error}"

        if image_path:
            # 清理 7 天前或超 50 张的旧图
            try:
                await self._cleanup_old_images()
            except Exception:
                pass
        return "群公告已发布"

    async def _cleanup_old_images(self, keep: int = 50, max_age_days: int = 7):
        def _do():
            try:
                d = self.cfg.group_notice_dir
                if not d.exists():
                    return
                now = time.time()
                files = [(p, p.stat().st_mtime) for p in d.iterdir() if p.is_file()]
                files.sort(key=lambda x: x[1])
                cutoff = now - max_age_days * 86400
                for p, mtime in list(files):
                    if mtime < cutoff or len(files) > keep:
                        try:
                            p.unlink(missing_ok=True)
                            files.remove((p, mtime))
                        except Exception:
                            pass
            except Exception as e:
                logger.debug(f"清理群公告图片失败: {e}")

        await asyncio.to_thread(_do)

    async def get_group_notice(self, event: AiocqhttpMessageEvent):
        """查看群公告"""
        notices = None
        last_error: Exception | None = None
        for action in ("_get_group_notice", "get_group_notice"):
            try:
                notices = await event.bot.call_action(action, group_id=int(event.get_group_id()))
                last_error = None
                break
            except Exception as e:
                last_error = e
        if last_error is not None:
            logger.warning(f"获取群公告失败: {last_error}")
            return "获取群公告失败"

        notices = unwrap_data(notices)
        if not isinstance(notices, list):
            return "当前群没有群公告"

        formatted_messages = []
        for notice in notices:
            if not isinstance(notice, dict):
                continue
            try:
                sender_id = notice.get("sender_id", "未知")
                publish_time = format_time(int(notice.get("publish_time", 0)), "%Y-%m-%d %H:%M:%S")
                message = notice.get("message", {}) or {}
                message_text = str(message.get("text", "")).replace("&#10;", "\n\n")
            except (TypeError, ValueError):
                continue

            formatted_message = f"【{publish_time}-{sender_id}】\n\n{textwrap.indent(message_text, '    ')}"
            formatted_messages.append(formatted_message)

        notices_str = "\n\n".join(formatted_messages)
        return notices_str or "当前群没有群公告"
