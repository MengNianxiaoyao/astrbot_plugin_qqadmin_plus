"""抽奖私聊问答：奖品 → 开奖方式 → 份数 → 发放 → 参与群 → 门槛。"""

import re
from typing import Any

from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.utils.session_waiter import (
    SessionController,
    SessionFilter,
    session_waiter,
)

from ...utils import send_lines
from .engine import LotteryConflictError
from .models import MAX_COPIES, MAX_GROUPS, MAX_PARTICIPANTS, WIZARD_TIMEOUT
from .parse import (
    CAP_PROMPT,
    COPIES_PROMPT,
    DEADLINE_PROMPT,
    DELIVERY_PROMPT,
    MODE_PROMPT,
    STOCK_PROMPT,
    THRESHOLD_PROMPT,
    TIME_ERROR_PROMPT,
    parse_deadline,
    parse_delivery,
    parse_draw_mode,
    parse_stock,
    parse_threshold,
    validate_cap,
    validate_copies,
    validate_prize,
)


class _WizardFilter(SessionFilter):
    """只把发起人的消息交给抽奖问答，其他人的消息照常走原流程。

    scope 为群号（群聊）或 unified_msg_origin（私聊），保证私聊发起时也能正确接管回答。
    """

    def __init__(self, scope: str, sender_id: str):
        self.scope = scope
        self.sender_id = sender_id
        self.session_id = f"lottery-wizard:{scope}:{sender_id}"

    def filter(self, event: AiocqhttpMessageEvent) -> str:
        scope = str(event.get_group_id() or "") or event.unified_msg_origin
        if scope == self.scope and str(event.get_sender_id()) == self.sender_id:
            return self.session_id
        return f"lottery-wizard-skip:{event.unified_msg_origin}:{event.get_sender_id()}"


class LotteryWizard:
    def __init__(self, handle):
        self.handle = handle

    async def start(self, event: AiocqhttpMessageEvent):
        h = self.handle
        await h.ensure_ready()
        group_id = str(event.get_group_id() or "")
        scope = group_id or event.unified_msg_origin
        if group_id and group_id in h.active:
            existing = h.active[group_id]
            await event.send(
                event.plain_result(f"本群已有抽奖进行中：{existing.prize}（{existing.label}），请等待开奖后再发起")
            )
            return
        if scope in h.wizards:
            await event.send(event.plain_result("正在创建抽奖，请先完成当前问答或发送「取消」"))
            return

        h.wizards.add(scope)
        locked = {scope}
        sender_id = str(event.get_sender_id())
        data: dict[str, Any] = {"bot_id": str(event.get_self_id())}
        step = {"name": "prize"}
        try:
            await event.send(event.plain_result("【发起抽奖】请输入奖品名称（发送「取消」退出）"))
        except Exception as e:
            # 引导消息发不出去时立即释放占位，避免该群永久停留在“创建中”
            h.wizards.difference_update(locked)
            logger.warning(f"抽奖引导发送失败: {e}")
            event.stop_event()
            return

        async def enter_groups(wait_event: AiocqhttpMessageEvent) -> bool:
            """拉取机器人已加入的群并发送选择列表；失败时返回 False 由调用方结束问答。"""
            candidates = await self._fetch_joined_groups(wait_event)
            if candidates is None:
                await wait_event.send(wait_event.plain_result("获取群列表失败，请重新发起抽奖"))
                return False
            if not candidates:
                await wait_event.send(wait_event.plain_result("机器人尚未加入任何群，无法发起抽奖"))
                return False
            data["candidates"] = candidates
            step["name"] = "groups"
            await self._send_group_choices(wait_event, candidates, group_id)
            return True

        @session_waiter(timeout=WIZARD_TIMEOUT)
        async def waiter(controller: SessionController, wait_event: AiocqhttpMessageEvent):
            text = wait_event.get_message_str().strip()
            if text in {"取消", "取消抽奖", "退出"}:
                await wait_event.send(wait_event.plain_result("已取消创建抽奖"))
                controller.stop()
                return

            if step["name"] == "prize":
                error = validate_prize(text)
                if error:
                    await wait_event.send(wait_event.plain_result(error))
                    controller.keep(WIZARD_TIMEOUT, reset_timeout=True)
                    return
                data["prize"] = " ".join(text.split())
                step["name"] = "mode"
                await wait_event.send(wait_event.plain_result(MODE_PROMPT))

            elif step["name"] == "mode":
                parsed = parse_draw_mode(text, h.tz)
                if parsed is None:
                    await wait_event.send(wait_event.plain_result(f"无法识别开奖方式。\n{MODE_PROMPT}"))
                    controller.keep(WIZARD_TIMEOUT, reset_timeout=True)
                    return
                data["mode"], data["expire_at"], data["target_count"], data["label"], data["duration"] = parsed
                data["max_participants"] = None
                step["name"] = "copies"
                await wait_event.send(wait_event.plain_result(COPIES_PROMPT))

            elif step["name"] == "copies":
                copies = validate_copies(text)
                if copies is None:
                    await wait_event.send(wait_event.plain_result(f"份数需为 1~{MAX_COPIES} 的整数，请重新输入"))
                    controller.keep(WIZARD_TIMEOUT, reset_timeout=True)
                    return
                data["copies"] = copies
                if data["mode"] == "instant":
                    step["name"] = "cap"
                    await wait_event.send(
                        wait_event.plain_result(CAP_PROMPT.format(copies=copies, max_cap=MAX_PARTICIPANTS))
                    )
                else:
                    step["name"] = "delivery"
                    await wait_event.send(wait_event.plain_result(DELIVERY_PROMPT))

            elif step["name"] == "cap":
                cap = validate_cap(text, data["copies"])
                if cap is None:
                    await wait_event.send(
                        wait_event.plain_result(
                            f"参与人数上限需为 {data['copies']}~{MAX_PARTICIPANTS} 的整数，请重新输入"
                        )
                    )
                    controller.keep(WIZARD_TIMEOUT, reset_timeout=True)
                    return
                data["max_participants"] = cap
                step["name"] = "deadline"
                await wait_event.send(wait_event.plain_result(DEADLINE_PROMPT))

            elif step["name"] == "deadline":
                parsed = parse_deadline(text, h.tz, "截止")
                if parsed is None:
                    await wait_event.send(wait_event.plain_result(TIME_ERROR_PROMPT))
                    controller.keep(WIZARD_TIMEOUT, reset_timeout=True)
                    return
                data["expire_at"], data["duration"], data["deadline_label"] = parsed
                step["name"] = "delivery"
                await wait_event.send(wait_event.plain_result(DELIVERY_PROMPT))

            elif step["name"] == "delivery":
                delivery = parse_delivery(text)
                if delivery is None:
                    await wait_event.send(wait_event.plain_result(f"无法识别发放方式。\n{DELIVERY_PROMPT}"))
                    controller.keep(WIZARD_TIMEOUT, reset_timeout=True)
                    return
                data["delivery"] = delivery
                if delivery == "manual":
                    data["stock"] = []
                    if not await enter_groups(wait_event):
                        controller.stop()
                        return
                else:
                    step["name"] = "stock"
                    await wait_event.send(wait_event.plain_result(STOCK_PROMPT.format(copies=data["copies"])))

            elif step["name"] == "stock":
                items, error = parse_stock(text, data["copies"])
                if error:
                    await wait_event.send(wait_event.plain_result(error))
                    controller.keep(WIZARD_TIMEOUT, reset_timeout=True)
                    return
                data["stock"] = items
                await wait_event.send(wait_event.plain_result(f"已存入 {len(items)} 份奖品。"))
                if not await enter_groups(wait_event):
                    controller.stop()
                    return

            elif step["name"] == "groups":
                groups, error = self._select_groups(text, data["candidates"], group_id)
                if error:
                    await wait_event.send(wait_event.plain_result(error))
                    controller.keep(WIZARD_TIMEOUT, reset_timeout=True)
                    return
                data["groups"] = groups
                locked.update(groups)
                h.wizards.update(groups)
                step["name"] = "threshold"
                await wait_event.send(wait_event.plain_result(THRESHOLD_PROMPT))

            else:
                parsed = parse_threshold(text)
                if parsed is None:
                    await wait_event.send(wait_event.plain_result(f"无法识别门槛。\n{THRESHOLD_PROMPT}"))
                    controller.keep(WIZARD_TIMEOUT, reset_timeout=True)
                    return
                data["min_level"], data["titles"] = parsed
                try:
                    lottery = await h.engine.create(group_id, sender_id, data, wait_event.bot)
                except LotteryConflictError as e:
                    # 选择期间群被另一场抽奖占用：解除旧选择，回到群选择步骤
                    old_groups = data.pop("groups", [])
                    locked.difference_update(old_groups)
                    h.wizards.difference_update(old_groups)
                    step["name"] = "groups"
                    await wait_event.send(
                        wait_event.plain_result(f"群 {'、'.join(e.groups)} 刚被其他抽奖占用，请重新选择参与群")
                    )
                    await self._send_group_choices(wait_event, data["candidates"], group_id)
                    controller.keep(WIZARD_TIMEOUT, reset_timeout=True)
                    return
                await h.save(immediate=True)
                await h.engine.announce_start(wait_event, lottery)
                controller.stop()
                return

            controller.keep(WIZARD_TIMEOUT, reset_timeout=True)

        try:
            await waiter(event, _WizardFilter(scope, sender_id))
        except TimeoutError:
            await event.send(event.plain_result("创建抽奖超时，已自动取消"))
        except Exception as e:
            logger.error(f"创建抽奖失败: {e}")
            await event.send(event.plain_result("创建抽奖失败，请稍后重试"))
        finally:
            h.wizards.difference_update(locked)
            event.stop_event()

    async def _fetch_joined_groups(self, event: AiocqhttpMessageEvent) -> list[tuple[str, str]] | None:
        """拉取本 Bot 已加入的群，当前群排第一位；失败返回 None。"""
        try:
            raw = await event.bot.get_group_list() or []
        except Exception as e:
            logger.warning(f"抽奖获取群列表失败: {e}")
            return None

        owner = str(event.get_group_id() or "")
        names: dict[str, str] = {}
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                gid = str(item.get("group_id") or "")
                if not gid.isdigit():
                    continue
                name = str(item.get("group_name") or item.get("group_remark") or gid).strip() or gid
                name = name.replace("\r", " ").replace("\n", " ")
                names[gid] = name[:20]
        if owner.isdigit():
            names.setdefault(owner, owner)
            others = sorted(((gid, name) for gid, name in names.items() if gid != owner), key=lambda item: int(item[0]))
            return [(owner, names[owner]), *others]
        return sorted(names.items(), key=lambda item: int(item[0]))

    async def _send_group_choices(
        self, event: AiocqhttpMessageEvent, candidates: list[tuple[str, str]], owner_group: str
    ):
        if owner_group:
            header = (
                f"请选择参与抽奖的群：回复列表中的序号或群号，多个用空格/逗号分隔，"
                f"最多 {MAX_GROUPS} 个（含本群，本群默认参与）。\n仅本群参与回复“无”。"
            )
        else:
            header = (
                f"请选择参与抽奖的群：回复列表中的序号或群号，多个用空格/逗号分隔，至少 1 个、最多 {MAX_GROUPS} 个。"
            )
        lines: list[str] = []
        for idx, (gid, name) in enumerate(candidates, 1):
            mark = "【本群】" if owner_group and gid == owner_group else ""
            lines.append(f"{idx}. {name}（{gid}）{mark}")
        await send_lines(event, header, lines, 40)

    def _select_groups(
        self, text: str, candidates: list[tuple[str, str]], owner_group: str
    ) -> tuple[list[str], str | None]:
        """从机器人已加入的群中按序号/群号选择；群聊发起时当前群默认参与，私聊发起时至少选一个群。"""
        if text in {"无", "本群", "仅本群"}:
            if not owner_group:
                return [], "私聊发起时请至少选择一个参与群"
            return [owner_group], None

        tokens = [token for token in re.split(r"[\s,，、]+", text) if token]
        if not tokens:
            return [], "请回复列表中的序号或群号；仅本群参与请回复“无”"

        index_map = {str(idx): gid for idx, (gid, _name) in enumerate(candidates, 1)}
        valid_ids = {gid for gid, _name in candidates}
        chosen: list[str] = [owner_group] if owner_group else []
        for token in tokens:
            if token.isdigit() and len(token) >= 5 and token in valid_ids:
                gid = token
            else:
                gid = index_map.get(token) or (token if token in valid_ids else None)
            if gid is None:
                return [], f"群 {token} 不在机器人已加入的群列表中，请回复列表里的序号或群号"
            if gid not in chosen:
                chosen.append(gid)

        if not chosen:
            return [], "请至少选择一个参与群"
        if len(chosen) > MAX_GROUPS:
            return [], f"一次最多选择 {MAX_GROUPS} 个群（含本群），请重新选择"
        busy = [
            gid for gid in chosen if gid != owner_group and (gid in self.handle.active or gid in self.handle.wizards)
        ]
        if busy:
            return [], f"群 {'、'.join(busy)} 已有进行中或创建中的抽奖，请换一个或等其结束"
        return chosen, None
