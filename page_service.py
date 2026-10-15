"""Web 面板业务层（无 HTTP 细节，可脱离 astrbot.api.web 单测）。

- Web 控制器只做参数解析/JSON 包装，校验与落库全在这里。
- 默认群（__default__）是模板：读 PluginConfig、写回 AstrBot 原生配置；
  普通群读 QQAdminDB 快照，follow_default=True 时删行回归跟随。
- _sanitize_value 按 _conf_schema.json 做类型清洗：bool/int 限幅、
  list 分词去空、带 options 只留合法值，非法回退当前值/默认值。
"""

import copy
import json
import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .config import PluginConfig
from .core.banpro import BanproHandle
from .core.join.approve import JoinApprover
from .core.join.state import PENDING_DEFAULT_DAYS, JoinState, pending_ttl_secs
from .data import QQAdminDB, QQAdminGlobalList
from .group_cache import QQGroupInfoCache
from .permission import perm_manager
from .utils import parse_bool

DEFAULT_GROUP_ID = "__default__"
FOLLOW_DEFAULT_KEY = "follow_default"


class QQAdminPageService:
    def __init__(
        self,
        cfg: PluginConfig,
        db: QQAdminDB,
        group_cache: QQGroupInfoCache,
        global_list: QQAdminGlobalList,
        banpro: BanproHandle,
        join_state: JoinState | None = None,
        context=None,
    ):
        self.cfg = cfg
        self.db = db
        self.group_cache = group_cache
        self.global_list = global_list
        self.banpro = banpro
        self.join_state = join_state
        self.context = context
        self.schema = self._load_schema(cfg.plugin_dir / "_conf_schema.json")

    @property
    def group_schema(self) -> dict[str, Any]:
        return {
            FOLLOW_DEFAULT_KEY: {
                "description": "跟随默认配置",
                "hint": "开启后，该群直接沿用默认群配置，下面的群专属配置项将不可编辑。",
                "type": "bool",
                "default": True,
            },
            **self.schema.get("default", {}).get("items", {}),
            **self._get_group_overlay_schema(),
        }

    def get_group_table(self) -> dict[str, dict[str, Any]]:
        """面板分组定义表：{组名: {hint, items}}，组顺序即面板展示顺序。"""
        groups = self.schema.get("groups", {})
        items = groups.get("items", {}) if isinstance(groups, dict) else {}
        table: dict[str, dict[str, Any]] = {}
        for name, entry in items.items():
            if not isinstance(entry, dict):
                continue
            members = entry.get("default", [])
            table[str(name)] = {
                "hint": entry.get("description", ""),
                "items": [str(m) for m in members] if isinstance(members, list) else [],
            }
        return table

    async def get_bootstrap_payload(self) -> dict[str, Any]:
        return {
            "schema": {
                "group": self.group_schema,
                "groups": self.get_group_table(),
            },
            "groups": await self.list_groups(),
            "refresh_error": self.group_cache.last_refresh_error,
        }

    def _default_group_summary(self) -> dict[str, Any]:
        """默认群共用的群信息骨架（列表条目与详情里的 group_info 共用）。"""
        return {
            "group_id": DEFAULT_GROUP_ID,
            "group_name": "默认群",
            "avatar": "",
            "member_count": 0,
            "max_member_count": 0,
        }

    def _default_group_config_values(self) -> dict[str, Any]:
        return {FOLLOW_DEFAULT_KEY: False, **copy.deepcopy(self.cfg.build_group_default_config())}

    def get_default_group_entry(self) -> dict[str, Any]:
        return {
            **self._default_group_summary(),
            "bot_role": "unknown",
            "is_default_group": True,
            "config": self._default_group_config_values(),
        }

    async def list_groups(
        self,
        force: bool = False,
        with_details: bool = False,
        prune: bool = True,
    ) -> list[dict[str, Any]]:
        groups = await self.group_cache.list_groups(force=force, with_details=with_details)
        return await self._build_group_entries(groups, prune=prune)

    async def list_groups_with_status(self, force: bool = False, with_details: bool = True) -> dict[str, Any]:
        """群列表 + 最近一次刷新错误，供面板展示刷新失败原因（手动同步默认附带实时详情校准人数）。"""
        groups = await self.list_groups(force=force, with_details=with_details, prune=False)
        return {"groups": groups, "refresh_error": self.group_cache.last_refresh_error}

    async def calibrate_groups(self) -> dict[str, Any]:
        """详情校准：只刷新缓存中已有群的实时人数/群名，不重拉整表、不删库；返回失败群供面板点名。"""
        groups, failed = await self.group_cache.refresh_details_only()
        entries = await self._build_group_entries(groups, prune=False)
        return {"groups": entries, "failed": failed}

    async def list_groups_with_bot_roles(self, force: bool = False, prune: bool = True) -> list[dict[str, Any]]:
        groups = await self.group_cache.list_groups_with_bot_roles(force_bot_roles=force)
        return await self._build_group_entries(groups, prune=prune)

    async def _build_group_entries(
        self,
        groups: list[dict[str, Any]],
        prune: bool = True,
    ) -> list[dict[str, Any]]:
        """prune 为 False 时只标记疑似失效（不展示）但不删库，供手动同步使用。"""
        result: list[dict[str, Any]] = [self.get_default_group_entry()]
        stale_group_ids: list[str] = []

        for group in groups:
            group_id = str(group.get("group_id", "")).strip()
            if self.group_cache.is_stale_group(group):
                stale_group_ids.append(group_id)
                continue
            result.append(
                {
                    **group,
                    "is_default_group": False,
                }
            )

        if prune:
            for group_id in stale_group_ids:
                await self.group_cache.delete_group_data(group_id)

        return result

    async def get_group_config(
        self,
        group_id: str,
        force: bool = False,
    ) -> dict[str, Any]:
        if str(group_id).strip() == DEFAULT_GROUP_ID:
            return self.get_default_group_config()

        group_id = self._normalize_group_id(group_id)
        follow_default = self.db.is_group_follow_default(group_id)
        group_info = await self.group_cache.get_group(group_id, force=force)
        if self.group_cache.is_stale_group(group_info):
            await self.group_cache.delete_group_data(group_id)
            raise ValueError(f"group {group_id} no longer exists and has been deleted")
        return {
            "group_id": group_id,
            "group_info": group_info,
            "config": {
                FOLLOW_DEFAULT_KEY: follow_default,
                **self.db.get_group_snapshot(group_id),
            },
            "is_default_group": False,
        }

    async def update_group_config(self, group_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if str(group_id).strip() == DEFAULT_GROUP_ID:
            return await self.update_default_group_config(payload)

        group_id = self._normalize_group_id(group_id)
        current = self.db.get_group_snapshot(group_id)
        follow_default_current = self.db.is_group_follow_default(group_id)
        sanitized = self._sanitize_value(
            payload,
            {"type": "object", "items": self.group_schema},
            {FOLLOW_DEFAULT_KEY: follow_default_current, **current},
        )
        follow_default = bool(sanitized.pop(FOLLOW_DEFAULT_KEY, True))
        if follow_default:
            await self.db.follow_default(group_id)
        else:
            await self.db.replace_group(group_id, sanitized)
        self.group_cache.invalidate(group_id)

        # 返回轻量结果：优先用列表缓存摘要，避免保存后又拉一次详情
        group_info = self.group_cache.get_cached_group(group_id)
        if group_info is None:
            group_info = await self.group_cache.get_group(group_id, force=False)
        if self.group_cache.is_stale_group(group_info):
            await self.group_cache.delete_group_data(group_id)
            raise ValueError(f"group {group_id} no longer exists and has been deleted")

        follow_default = self.db.is_group_follow_default(group_id)
        return {
            "group_id": group_id,
            "group_info": group_info,
            "config": {
                FOLLOW_DEFAULT_KEY: follow_default,
                **self.db.get_group_snapshot(group_id),
            },
            "is_default_group": False,
        }

    async def reset_group_config(self, group_id: str) -> dict[str, Any]:
        if str(group_id).strip() == DEFAULT_GROUP_ID:
            raise ValueError("default group does not support reset")

        group_id = self._normalize_group_id(group_id)
        await self.db.follow_default(group_id)
        self.group_cache.invalidate(group_id)
        return await self.get_group_config(group_id)

    def get_default_group_config(self) -> dict[str, Any]:
        return {
            "group_id": DEFAULT_GROUP_ID,
            "group_info": self._default_group_summary(),
            "config": self._default_group_config_values(),
            "is_default_group": True,
        }

    async def update_default_group_config(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        current = copy.deepcopy(self.cfg.build_group_default_config())
        sanitized = self._sanitize_value(
            payload,
            {"type": "object", "items": self.group_schema},
            {FOLLOW_DEFAULT_KEY: False, **current},
        )
        sanitized.pop(FOLLOW_DEFAULT_KEY, None)
        self._apply_group_level_updates(sanitized)
        self.db.default_cfg = self.cfg.build_group_default_config()
        self.cfg.refresh_runtime_settings()
        self.cfg.save_config()
        self.group_cache.invalidate()
        return self.get_default_group_config()

    async def get_global_lists(self) -> dict[str, list[str]]:
        return {name: self.global_list.get(name) for name in ("allow", "block")}

    async def update_global_list(self, list_type: str, items: list[str]) -> list[str]:
        if not isinstance(items, list):
            raise ValueError("items must be a list")
        cleaned = [str(item).strip() for item in items if str(item).strip().isdigit()]
        return self.global_list.set(list_type, cleaned)

    async def get_global_ban_words(self) -> dict[str, list[str]]:
        return {
            "global": self.banpro.get_global_ban_words(),
            "builtin": self.banpro.get_available_builtin_ban_words(),
            "builtin_version": self.banpro.builtin_ban_version,
        }

    async def update_global_ban_words(self, words: list[str]) -> list[str]:
        if not isinstance(words, list):
            raise ValueError("words must be a list")
        return self.banpro.set_global_ban_words(words)

    async def import_builtin_ban_words(self, words: list[str]) -> list[str]:
        if not isinstance(words, list):
            raise ValueError("words must be a list")
        return self.banpro.import_builtin_ban_words(words)

    async def restore_builtin_ban_words(self) -> list[str]:
        return self.banpro.restore_builtin_ban_words()

    def _require_join_state(self) -> JoinState:
        if self.join_state is None:
            raise ValueError("待审核队列不可用")
        return self.join_state

    async def _pending_ttl(self, gid: str) -> float:
        """某群待审核新鲜期（秒），与审核判定共用同一换算。"""
        return pending_ttl_secs(await self.db.get(gid, "join_pending_ttl", PENDING_DEFAULT_DAYS))

    async def _scan_pending(self) -> list[tuple[str, dict[str, Any], float, bool]]:
        """全量待审核登记 [(通知ID, 登记, 该群新鲜期, 是否新鲜)]，同群 ttl 只查一次库。"""
        state = self._require_join_state()
        now = time.time()
        ttl_cache: dict[str, float] = {}
        scanned = []
        for key, r in state.pending.items():
            rgid = str(r.get("gid") or "")
            if rgid not in ttl_cache:
                ttl_cache[rgid] = await self._pending_ttl(rgid)
            ttl = ttl_cache[rgid]
            scanned.append((key, r, ttl, now - r.get("ts", 0) < ttl))
        return scanned

    def _pending_group_name(self, gid: str) -> str:
        """待审核列表只配看个群名：只读列表缓存，零网络请求；缓存未命中直接显示群号。"""
        try:
            group = self.group_cache.get_cached_group(gid)
            name = (group or {}).get("group_name", "")
            if name:
                return str(name)
        except Exception:
            pass
        return gid

    async def list_pending_approvals(self, gid: str | None = None) -> list[dict[str, Any]]:
        """待审核队列：默认全群，可按群过滤；按申请时间排序，附新鲜标记。
        先从落盘重读，面板“刷新”可直接看到手改 json 文件的结果。"""
        state = self._require_join_state()
        state.reload_pending()
        if gid:
            gid = self._normalize_group_id(gid)
        items: list[dict[str, Any]] = []
        for key, r, _ttl, fresh in await self._scan_pending():
            rgid = str(r.get("gid") or "")
            if gid and rgid != gid:
                continue
            items.append(
                {
                    "key": key,
                    "gid": rgid,
                    "group_name": self._pending_group_name(rgid),
                    "uid": str(r.get("uid") or ""),
                    "nickname": str(r.get("nickname") or ""),
                    "comment": str(r.get("comment") or ""),
                    "ts": r.get("ts", 0),
                    "fresh": fresh,
                }
            )
        items.sort(key=lambda i: i["ts"])
        return items

    async def drop_pending_entry(self, key: str) -> int:
        """清除单条待审核登记（面板手动清积压，不触达 QQ 侧申请）。"""
        state = self._require_join_state()
        if not key or key not in state.pending:
            raise ValueError("待审核记录不存在")
        return state.drop_pending(lambda k, r: k == key)

    async def drop_pending_stale(self) -> int:
        """清除所有已过新鲜期的待审核登记（各群按自家有效期判定）。"""
        state = self._require_join_state()
        stale_set = {key for key, _r, _ttl, fresh in await self._scan_pending() if not fresh}
        return state.drop_pending(lambda k, r: k in stale_set)

    def _resolve_qq_bots(self, platform_id: str | None) -> list:
        """按 platform_id 定位 bot；老数据无 platform_id 时退化为全部 aiocqhttp 实例。"""
        if self.context is None:
            raise ValueError("面板后端未接管平台，无法审批")
        candidates = []
        get_inst = getattr(self.context, "get_platform_inst", None)
        if platform_id and callable(get_inst):
            platform = get_inst(platform_id)
            if platform is not None:
                candidates = [platform]
        if not candidates:
            manager = getattr(self.context, "platform_manager", None)
            insts = getattr(manager, "platform_insts", None) or []
            for platform in insts:
                try:
                    if getattr(platform.meta(), "name", "") == "aiocqhttp":
                        candidates.append(platform)
                except Exception:
                    continue
        bots = []
        for platform in candidates:
            get_client = getattr(platform, "get_client", None)
            bot = get_client() if callable(get_client) else getattr(platform, "bot", None)
            if bot is not None:
                bots.append(bot)
        if not bots:
            raise ValueError("找不到可用的 QQ 机器人实例")
        return bots

    async def settle_pending(self, key: str, approve: bool, reason: str = "") -> str:
        """面板直接批驳：经 context 定位 bot 调 OneBot 进群审批接口，落定后清登记。
        多 bot 时逐个尝试；站外已处理视为落定并对账。"""
        state = self._require_join_state()
        entry = state.pending.get(key)
        if not entry:
            raise ValueError("待审核记录不存在")
        flag = str(entry.get("flag") or "")
        nickname = str(entry.get("nickname") or "") or "该用户"
        if not flag:
            raise ValueError("该记录缺少 flag，请在群内处理")
        last_err: Exception | None = None
        for bot in self._resolve_qq_bots(entry.get("platform_id")):
            try:
                await bot.set_group_add_request(flag=flag, sub_type="add", approve=approve, reason="" if approve else reason)
            except Exception as e:
                err = str(e).lower()
                if "already agree" in err or "already refuse" in err or "already disagree" in err:
                    state.drop_pending(lambda k, r: r.get("flag") == flag)
                    return JoinApprover._approve_fail_hint(e)
                last_err = e
                continue
            state.drop_application_by_flag(flag)
            state.drop_pending(lambda k, r: r.get("flag") == flag)
            if approve:
                return f"已同意{nickname}进群"
            return f"已拒绝{nickname}进群" + (f"\n理由：{reason}" if reason else "")
        raise ValueError(f"审批失败：{last_err}" if last_err else "审批失败")

    @staticmethod
    def _load_schema(schema_path: Path) -> dict[str, Any]:
        try:
            return json.loads(schema_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("加载页面 schema 失败: %s", exc)
            return {}

    @staticmethod
    def _normalize_group_id(group_id: str | int | None) -> str:
        gid = str(group_id or "").strip()
        if not gid or not gid.isdigit():
            raise ValueError("group_id must be a numeric string")
        return gid

    def _apply_group_level_updates(self, updated: dict[str, Any]) -> None:
        default_fields = self.schema.get("default", {}).get("items", {})
        default_updates = {key: value for key, value in updated.items() if key in default_fields}
        self._merge_dict(self.cfg.default, default_updates)

        for key in ("admin_audit", "random_ban_time", "llm_get_msg_count"):
            if key in updated:
                setattr(self.cfg, key, updated[key])
        if "vote_ban" in updated:
            self.cfg.vote_ban.ttl = updated["vote_ban"]["ttl"]
            self.cfg.vote_ban.threshold = updated["vote_ban"]["threshold"]
        if "perms" in updated:
            self._merge_dict(self.cfg.perms, updated["perms"])

        perm_manager.refresh(self.cfg, self.db)

    def _get_group_overlay_schema(self) -> dict[str, Any]:
        keys = [
            "admin_audit",
            "random_ban_time",
            "vote_ban",
            "llm_get_msg_count",
            "perms",
        ]
        return {key: copy.deepcopy(self.schema[key]) for key in keys if key in self.schema}

    @staticmethod
    def _merge_dict(target: dict[str, Any], source: dict[str, Any] | None) -> None:
        if source is None:
            return
        target.clear()
        target.update(copy.deepcopy(source))

    def _sanitize_value(
        self,
        value: Any,
        schema: dict[str, Any],
        current: Any = None,
    ) -> Any:
        field_type = schema.get("type", "string")

        if field_type == "object":
            items = schema.get("items", {})
            payload = value if isinstance(value, dict) else {}
            current_map = current if isinstance(current, dict) else {}
            result: dict[str, Any] = {}
            for key, child_schema in items.items():
                child_current = current_map.get(key, child_schema.get("default"))
                child_value = payload[key] if key in payload else child_current
                result[key] = self._sanitize_value(child_value, child_schema, child_current)
            return result

        if field_type == "bool":
            parsed = parse_bool(value)
            if parsed is None:
                raise ValueError(f"invalid bool value: {value}")
            return parsed

        if field_type == "int":
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                try:
                    parsed = int(current)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    parsed = int(schema.get("default", 0))
            slider = schema.get("slider", {})
            minimum = slider.get("min")
            maximum = slider.get("max")
            if minimum is not None:
                parsed = max(int(minimum), parsed)
            if maximum is not None:
                parsed = min(int(maximum), parsed)
            return parsed

        if field_type == "list":
            if value is None:
                return []
            if isinstance(value, str):
                items = re.split(r"[\s\n,，]+", value)
            elif isinstance(value, list):
                items = value
            else:
                raise ValueError(f"invalid list value: {value}")
            result = [str(item).strip() for item in items if str(item).strip()]
            # 带固定选项的列表（复选框组）只保留合法选项，防止脏数据
            options = schema.get("options")
            if options:
                allowed = set()
                for opt in options:
                    if isinstance(opt, dict):
                        allowed.add(str(opt.get("value", "")))
                    else:
                        allowed.add(str(opt))
                result = [item for item in result if item in allowed]
            return result

        options = schema.get("options")
        parsed = str(value or "")
        if options and parsed not in options:
            return str(current if current is not None else schema.get("default", ""))
        return parsed
