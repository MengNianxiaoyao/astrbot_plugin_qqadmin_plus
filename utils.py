"""共享小工具：JSON 原子读写、群成员/昵称、分片发送、At/引用解析、名单与 CQ 码解析。

- safe_int / load_json / save_json_atomic：跨模块统一的转换与文件 IO。
- get_group_member / get_nickname：群资料带超时，昵称自动降级陌生人资料。
- send_lines：按行分片发送，一行内容不会按字数截断。
- get_ats 排除 Bot 自身；parse_bool 认“开/关/on/off/1/0”等，非法回 default。
- parse_cq_to_chain 只认 at/image，未知类型/非法参数保留原文不丢弃；
  本地图片必须落在 allowed_roots 内，否则拦截为占位符（防任意文件读取）。
"""

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import anyio
from aiohttp import ClientSession
from astrbot.api import logger
from astrbot.core.message.components import At, BaseMessageComponent, Image, Plain, Reply
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)


def safe_int(value: Any, default: int | None = 0) -> int | None:
    """尽力转 int，None/空串/非法值回 default。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_json(path: Path, default: Any = None) -> Any:
    """读取 JSON；文件不存在或损坏时返回 default。"""
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"读取 JSON 失败({path}): {e}")
    return default


def save_json_atomic(path: Path, data: Any, indent: int | None = 2) -> None:
    """先写临时文件再替换，避免写入中断留下半个 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=indent), encoding="utf-8")
    temp.replace(path)


async def get_group_member(
    client: Any,
    group_id: str | int | None,
    user_id: str | int | None,
    timeout_secs: float = 10.0,
    no_cache: bool = False,
) -> dict:
    """取群成员资料（等级/头衔/名片）；超时或失败返回空 dict。"""
    gid = str(group_id or "")
    uid = str(user_id or "")
    if client is None or not gid.isdigit() or not uid.isdigit():
        return {}
    try:
        try:
            kwargs = {"no_cache": True} if no_cache else {}
            info = await asyncio.wait_for(
                client.get_group_member_info(group_id=int(gid), user_id=int(uid), **kwargs),
                timeout_secs,
            )
        except TypeError:
            if not no_cache:
                raise
            # 部分 OneBot 实现不认 no_cache，退回普通查询
            info = await asyncio.wait_for(
                client.get_group_member_info(group_id=int(gid), user_id=int(uid)),
                timeout_secs,
            )
    except Exception as e:
        logger.debug(f"获取群成员资料失败({gid}/{uid}): {e}")
        return {}
    return info if isinstance(info, dict) else {}


async def get_nickname(
    event: AiocqhttpMessageEvent,
    user_id: int | str,
    info: dict | None = None,
    timeout_secs: float = 10.0,
) -> str:
    """获取指定群友的群昵称或 Q 名，群接口失败/空结果自动降级到陌生人资料。

    info 可由调用方预先取好（例如抽奖门槛已经查过群资料），避免重复请求。
    """
    uid = int(user_id)
    client = event.bot
    if info is None:
        info = await get_group_member(client, event.get_group_id(), uid, timeout_secs=timeout_secs)

    # 群资料没拿到就降级到陌生人资料
    if not info:
        try:
            info = await asyncio.wait_for(client.get_stranger_info(user_id=uid), timeout_secs) or {}
        except Exception:
            info = {}

    # 依次取群名片、QQ 昵称、通用 nick，兜底数字 UID
    return info.get("card") or info.get("nickname") or info.get("nick") or str(uid)


def chunk_lines(title: str, lines: list[str], chunk_size: int = 50) -> list[str]:
    """按行数分片：一行内容始终完整保留，绝不按字数截断。"""
    total = len(lines)
    texts: list[str] = []
    for start in range(0, total, chunk_size):
        chunk = lines[start : start + chunk_size]
        head = title if start == 0 else f"（续 {start + 1}-{start + len(chunk)}/{total}）"
        texts.append(f"{head}\n" + "\n".join(chunk))
    return texts


async def send_lines(
    event: AiocqhttpMessageEvent,
    title: str,
    lines: list[str],
    chunk_size: int = 50,
):
    """主动发送场景直接发群；命令回执请用 chunk_lines 返回给入口 yield。"""
    for text in chunk_lines(title, lines, chunk_size):
        await event.send(event.plain_result(text))


async def resolve_allow_ids(db, global_list, gid: str) -> list:
    """取本群进群白名单（全局开关开启时返回全局白名单）"""
    if await db.get(gid, "use_global_allow", False):
        return list(global_list.get("allow"))
    return await db.get(gid, "allow_ids", [])


async def resolve_block_ids(db, global_list, gid: str) -> list:
    """取本群进群黑名单（全局开关开启时返回全局黑名单）"""
    if await db.get(gid, "use_global_block", False):
        return list(global_list.get("block"))
    return await db.get(gid, "block_ids", [])


def get_ats(event: AiocqhttpMessageEvent) -> list[str]:
    """获取被 at 者们的 id 列表（排除 Bot 自身与 @全体成员）"""
    return [
        str(seg.qq)
        for seg in event.get_messages()
        if isinstance(seg, At) and str(seg.qq) not in {str(event.get_self_id()), "all"}
    ]


def is_group_admin(event: AiocqhttpMessageEvent) -> bool:
    """群主/管理员判定。

    部分 AstrBot 版本不会把 OneBot sender.role 写回 event.role，导致
    `event.is_admin()` 在 aiocqhttp 下恒为 False；这里直接读原始事件里的
    sender.role，避免把管理员的发言当成刷屏/违禁词误处理。
    """
    raw = getattr(event.message_obj, "raw_message", None)
    sender = raw.get("sender") if isinstance(raw, dict) else None
    role = str((sender or {}).get("role", "")).lower()
    return role in {"owner", "admin"}


def get_reply_message_str(event: AiocqhttpMessageEvent) -> str | None:
    """
    获取被引用的消息解析后的纯文本消息字符串。
    """
    return next(
        (seg.message_str for seg in event.message_obj.message if isinstance(seg, Reply)),
        "",
    )


def get_reply_message_id(event: AiocqhttpMessageEvent) -> str | None:
    """获取被引用消息的消息ID，用于精确匹配进群申请通知等引用消息。"""
    for seg in event.get_messages():
        if isinstance(seg, Reply) and seg.id:
            return str(seg.id)
    return None


def get_replyer_id(event: AiocqhttpMessageEvent) -> str | None:
    """获取被引用消息发送者的id（排除机器人自己）"""
    for seg in event.get_messages():
        if isinstance(seg, Reply) and seg.sender_id and str(seg.sender_id) != str(event.get_self_id()):
            return str(seg.sender_id)
    return None


def get_target_ids(event: AiocqhttpMessageEvent) -> list[str]:
    """操作目标：优先取 @ 的群友，没有则取被引用消息的发送者。"""
    tids = get_ats(event)
    if tids:
        return tids
    replyer_id = get_replyer_id(event)
    return [replyer_id] if replyer_id else []


def extract_message_id(result) -> str | None:
    """从 send 类接口的返回结果中提取消息ID（兼容 dict / 带属性对象 / None 多种形态）。"""
    if result is None:
        return None
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, dict) and data.get("message_id"):
            return str(data["message_id"])
        if result.get("message_id"):
            return str(result["message_id"])
    mid = getattr(result, "message_id", None)
    return str(mid) if mid else None


def format_time(timestamp, fmt: str = "%Y-%m-%d", tz=None):
    """格式化时间戳；默认本地日期。"""
    return datetime.fromtimestamp(timestamp, tz).strftime(fmt)


async def download_file(
    url: str, save_path: Path, max_size: int = 10 * 1024 * 1024, timeout_secs: int = 10
) -> Path | None:
    """下载文件并保存到本地。优先使用原协议，失败后回退到另一种协议（http/https 互转）。带超时与大小限制。"""
    candidates = [url]
    if url.startswith("https://"):
        candidates.append("http://" + url[len("https://") :])
    elif url.startswith("http://"):
        candidates.append("https://" + url[len("http://") :])

    async with ClientSession() as client:
        for candidate in candidates:
            try:
                response = await client.get(candidate, timeout=timeout_secs)
                response.raise_for_status()
                # 预检 Content-Length
                clen = response.headers.get("Content-Length")
                if clen and clen.isdigit() and int(clen) > max_size:
                    logger.warning(f"文件过大拒绝下载({candidate} {clen} > {max_size})")
                    continue
                file = await response.read()
                if len(file) > max_size:
                    logger.warning(f"文件过大拒绝保存({candidate} {len(file)} > {max_size})")
                    continue

                await anyio.Path(save_path).parent.mkdir(parents=True, exist_ok=True)

                async with await anyio.open_file(save_path, "wb") as img_file:
                    await img_file.write(file)

                logger.info(f"文件已保存: {save_path}")
                return save_path
            except Exception as e:
                logger.error(f"文件下载失败({candidate}): {e}")

    return None


def extract_image_url(chain: list[BaseMessageComponent]) -> str | None:
    """从消息链中提取图片URL"""
    for seg in chain:
        if isinstance(seg, Image):
            return seg.url
        elif isinstance(seg, Reply) and seg.chain:
            for reply_seg in seg.chain:
                if isinstance(reply_seg, Image):
                    return reply_seg.url
    return None


def parse_clock(value: str) -> tuple[int, int] | None:
    """解析 HH:MM 时刻（兼容中文冒号），非法返回 None。"""
    parts = value.strip().replace("：", ":").split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return None
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def parse_bool(mode: str | bool | None, default: bool | None = False):
    """解析布尔值；输入为 None 时返回 None，无法识别时返回 default（可传 None 表示保留原值）"""
    if mode is None:
        return None
    if isinstance(mode, bool):
        return mode
    mode = str(mode).strip().lower()
    match mode:
        case "开" | "开启" | "启用" | "on" | "true" | "1" | "是" | "真":
            return True
        case "关" | "关闭" | "禁用" | "off" | "false" | "0" | "否" | "假":
            return False
        case _:
            return default


# 匹配 [CQ:type] 或 [CQ:type,param=val,...]; param 部分可选,兼容无参 CQ
_CQ_PATTERN = re.compile(r"\[CQ:(\w+)(?:,([^\]]*))?\]")

# 允许的本地图片根目录由调用方传入时校验;默认仅允许 data/plugin 目录


def parse_cq_to_chain(text: str, allowed_roots: list[Path] | None = None) -> list:
    """将含 CQ 码的文本解析为 AstrBot 消息组件列表.

    支持:
      - [CQ:at,qq=123] -> At
      - [CQ:image,file=.../url=...] -> Image (http(s) 走 fromURL, 本地走 fromFileSystem)
    未识别的 CQ 类型、参数缺失/非法时保留为 Plain 原文,避免静默丢弃.
    本地图片不存在时记录日志并插入通用占位 Plain,不暴露文件系统路径.
    """
    if not text:
        return []

    chain: list = []
    last_pos = 0

    for match in _CQ_PATTERN.finditer(text):
        # 前缀纯文本
        plain_text = text[last_pos : match.start()]
        if plain_text:
            chain.append(Plain(plain_text))

        raw = match.group(0)
        cq_type = match.group(1)
        params_str = match.group(2) or ""

        params: dict[str, str] = {}
        if params_str:
            for item in params_str.split(","):
                if "=" in item:
                    k, v = item.split("=", 1)
                    # CQ 码中 &amp; 为 & 的转义
                    v = v.replace("&amp;", "&")
                    params[k.strip()] = v.strip()

        if cq_type == "at":
            qq = (params.get("qq") or "").strip()
            if qq and qq.isdigit():
                chain.append(At(qq=qq, name=""))
            else:
                # qq 缺失或非法:保留原文便于发现配置问题
                logger.warning(f"CQ at 缺少合法 qq 参数,保留原文: {raw}")
                chain.append(Plain(raw))
        elif cq_type == "image":
            file_path = params.get("file") or params.get("url") or ""
            file_path = file_path.strip()
            if not file_path:
                logger.warning(f"CQ image 缺少 file/url 参数,保留原文: {raw}")
                chain.append(Plain(raw))
            elif file_path.startswith("http://") or file_path.startswith("https://"):
                try:
                    chain.append(Image.fromURL(file_path))
                except Exception as e:
                    logger.warning(f"CQ image URL 解析失败: {e}, 原文: {raw}")
                    chain.append(Plain(raw))
            else:
                # 本地路径: 必须限制在 allowed_roots 内，未传入则默认拒绝，防止任意文件读取
                p = Path(file_path)
                try:
                    resolved = p.resolve()
                    if not allowed_roots:
                        logger.warning(f"CQ image 未配置可访问目录已拦截: {raw}")
                        chain.append(Plain("[图片加载失败]"))
                        last_pos = match.end()
                        continue

                    def _is_allowed(path: Path) -> bool:
                        for root in allowed_roots:
                            try:
                                if path.is_relative_to(root.resolve()):
                                    return True
                            except AttributeError:
                                # py<3.9 fallback
                                try:
                                    path.relative_to(root.resolve())
                                    return True
                                except ValueError:
                                    continue
                            except ValueError:
                                continue
                        return False

                    if not _is_allowed(resolved):
                        logger.warning(f"CQ image 越权访问已拦截: {raw}")
                        chain.append(Plain("[图片加载失败]"))
                        last_pos = match.end()
                        continue
                    if p.exists() and p.is_file():
                        chain.append(Image.fromFileSystem(str(resolved)))
                    else:
                        logger.warning(f"CQ image 本地文件不存在,已省略: {raw}")
                        chain.append(Plain("[图片加载失败]"))
                except Exception as e:
                    logger.warning(f"CQ image 本地文件处理失败: {e}")
                    chain.append(Plain("[图片加载失败]"))
        else:
            # 未知类型:保留原文,避免丢弃未来扩展的 CQ
            chain.append(Plain(raw))

        last_pos = match.end()

    # 尾段纯文本
    rest = text[last_pos:]
    if rest:
        chain.append(Plain(rest))

    return chain
