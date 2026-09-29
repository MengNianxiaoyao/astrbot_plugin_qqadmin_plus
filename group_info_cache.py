from __future__ import annotations

import asyncio
import copy
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.star import Context
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (
    AiocqhttpAdapter,
)

from .data import QQAdminDB

BOT_ROLE_PRIORITY = {
    "owner": 0,
    "admin": 1,
    "member": 2,
    "unknown": 2,
}

# OneBot 单次调用超时封顶（秒）：防实现端无响应时无限挂起，与跨群探测的超时策略一致
API_TIMEOUT_SECONDS = 10.0


class QQGroupInfoCache:
    def __init__(
        self,
        context: Context,
        db: QQAdminDB,
        ttl_seconds: int = 90,
    ):
        self.context = context
        self.db = db
        self.ttl_seconds = ttl_seconds
        self.role_ttl_seconds = 600

        self._lock = asyncio.Lock()
        self._bot_role_lock = asyncio.Lock()
        self._last_refresh_at = 0.0
        self._last_refresh_error: str | None = None
        self._group_list_cache: list[dict[str, Any]] = []
        self._group_detail_cache: dict[str, dict[str, Any]] = {}
        self._group_detail_ts: dict[str, float] = {}
        self._group_clients: dict[str, Any] = {}
        self._bot_role_cache: dict[str, str] = {}
        self._bot_role_ts: dict[str, float] = {}
        self._client_bot_ids: dict[int, str] = {}

    async def list_groups(self, force: bool = False) -> list[dict[str, Any]]:
        if force or not self._is_fresh() or not self._group_list_cache:
            await self._refresh_group_list(force=force)
        return copy.deepcopy(self._group_list_cache)

    async def list_groups_with_bot_roles(
        self,
        force_bot_roles: bool = False,
    ) -> list[dict[str, Any]]:
        if not self._is_fresh() or not self._group_list_cache:
            await self._refresh_group_list(force=False)
        await self._hydrate_bot_roles(force=force_bot_roles)
        return copy.deepcopy(self._group_list_cache)

    async def get_group(self, group_id: str, force: bool = False) -> dict[str, Any]:
        normalized_group_id = str(group_id).strip()
        if not normalized_group_id:
            raise ValueError("group_id must not be empty")

        # 仅当群不在缓存的列表里时，才刷新一次全量列表（新增/失效群兜底），
        # 避免切换群时频繁触发整表重建。
        if self._find_group_from_cache(normalized_group_id) is None:
            if force or not self._group_list_cache or not self._is_fresh():
                await self._refresh_group_list(force=force)

        now = time.time()
        cached_detail = self._group_detail_cache.get(normalized_group_id)
        if cached_detail and not force and (now - self._group_detail_ts.get(normalized_group_id, 0)) < self.ttl_seconds:
            return copy.deepcopy(cached_detail)

        detail = await self._load_group_detail(normalized_group_id)
        self._group_detail_cache[normalized_group_id] = detail
        self._group_detail_ts[normalized_group_id] = now
        return copy.deepcopy(detail)

    def get_cached_group(self, group_id: str) -> dict[str, Any] | None:
        """从列表缓存中取群摘要（不触发任何网络请求）。"""
        return self._find_group_from_cache(str(group_id).strip())

    def invalidate(self, group_id: str | None = None) -> None:
        if group_id:
            gid = str(group_id).strip()
            self._group_detail_cache.pop(gid, None)
            self._group_detail_ts.pop(gid, None)
            return
        self._group_detail_cache.clear()
        self._group_detail_ts.clear()

    def remove_group(self, group_id: str | None) -> None:
        normalized_group_id = str(group_id or "").strip()
        if not normalized_group_id:
            return

        self._group_list_cache = [item for item in self._group_list_cache if item.get("group_id") != normalized_group_id]
        self._group_detail_cache.pop(normalized_group_id, None)
        self._group_detail_ts.pop(normalized_group_id, None)
        self._group_clients.pop(normalized_group_id, None)
        self._bot_role_cache.pop(normalized_group_id, None)
        self._bot_role_ts.pop(normalized_group_id, None)

    def _is_fresh(self) -> bool:
        return (time.time() - self._last_refresh_at) < self.ttl_seconds

    async def _refresh_group_list(self, force: bool = False) -> None:
        async with self._lock:
            if not force and self._is_fresh() and self._group_list_cache:
                return

            merged_groups: dict[str, dict[str, Any]] = {}
            group_clients: dict[str, Any] = {}
            missing_detail_group_ids: set[str] = set()
            refresh_errors: list[str] = []

            clients = self._iter_clients()
            if not clients:
                logger.warning("No aiocqhttp client available, skip QQ group list refresh and use cached groups only")

            # 多 client 并行拉取，总耗时取最慢者（仍受单次超时封顶），而非顺序累加
            loaded = await asyncio.gather(
                *(self._fetch_group_list(index, client) for index, client in enumerate(clients))
            )
            for _index, client, items, error in loaded:
                if error is not None:
                    refresh_errors.append(error)
                    continue
                for item in items:
                    group_id = str(item.get("group_id", "")).strip()
                    if not group_id or group_id in merged_groups:
                        continue
                    merged_groups[group_id] = self._normalize_group_summary(item)
                    group_clients[group_id] = client
                    if self._needs_detail_refresh(item, group_id):
                        missing_detail_group_ids.add(group_id)

            for group_id in self.db.list_group_ids():
                if group_id not in merged_groups:
                    merged_groups[group_id] = self._build_fallback_group(group_id)
                    missing_detail_group_ids.add(group_id)

            if missing_detail_group_ids:
                await self._hydrate_missing_groups(merged_groups, group_clients, missing_detail_group_ids)

            live_count = sum(1 for group in merged_groups.values() if group.get("source") == "live")
            if not clients:
                self._last_refresh_error = "群列表刷新失败：无可用的 aiocqhttp 连接，请检查机器人是否在线"
            elif live_count == 0 and refresh_errors:
                detail = "; ".join(refresh_errors)
                self._last_refresh_error = f"群列表刷新失败：{detail[:500]}"
            else:
                self._last_refresh_error = None

            groups = list(merged_groups.values())
            self._attach_cached_bot_roles(groups)
            self._group_list_cache = self._sort_groups(groups)
            self._group_clients = group_clients
            self._last_refresh_at = time.time()

    @property
    def last_refresh_error(self) -> str | None:
        """最近一次群列表刷新的错误（无错误返回 None），供 Web 面板展示。"""
        return self._last_refresh_error

    @staticmethod
    async def _call_action(client: Any, action: str, **kwargs: Any) -> Any:
        """带超时封顶的 OneBot 调用，超时抛 TimeoutError 由各调用处按失败处理。"""
        return await asyncio.wait_for(client.call_action(action, **kwargs), timeout=API_TIMEOUT_SECONDS)

    def _describe_client(self, index: int, client: Any) -> str:
        """给 client 一个可读标识（优先用已缓存的 bot QQ，便于多开时定位）。"""
        bot_id = self._client_bot_ids.get(id(client))
        return f"client#{index}(bot {bot_id})" if bot_id else f"client#{index}"

    @staticmethod
    def _format_error(exc: BaseException) -> str:
        """异常类型 + 信息双保险：str(exc) 为空时回退到 repr，避免日志只剩一个冒号。"""
        message = str(exc).strip() or repr(exc)
        return f"{type(exc).__name__}: {message}"

    async def _fetch_group_list(self, index: int, client: Any) -> tuple[int, Any, list[dict[str, Any]], str | None]:
        """单个 client 拉取群列表，返回 (序号, client, 群条目, 错误信息)。"""
        label = self._describe_client(index, client)
        try:
            result = await self._call_action(client, "get_group_list")
        except Exception as exc:
            formatted = self._format_error(exc)
            logger.warning("Failed to load QQ group list via %s: %s", label, formatted)
            return index, client, [], f"{label}: {formatted}"
        items = self._extract_list(result)
        if not items and not (isinstance(result, list) or (isinstance(result, dict) and isinstance(result.get("data"), list))):
            logger.warning("QQ group list via %s returned unexpected result: %s", label, repr(result)[:500])
            return index, client, [], f"{label}：接口返回异常"
        return index, client, items, None

    async def _race_clients(self, attempts: list[tuple[int, Any, Any]]) -> tuple[Any | None, Any | None, list[str]]:
        """多 client 并行竞速，返回 (首个成功结果, 对应 client, 失败记录)；全部失败返回 (None, None, errors)。"""
        if not attempts:
            return None, None, []
        tasks = {asyncio.ensure_future(coro): (index, client) for index, client, coro in attempts}
        errors: list[str] = []
        pending = set(tasks)
        try:
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    index, client = tasks[task]
                    try:
                        return task.result(), client, errors
                    except Exception as exc:
                        errors.append(f"{self._describe_client(index, client)}: {self._format_error(exc)}")
        finally:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        return None, None, errors

    async def _load_group_detail(self, group_id: str) -> dict[str, Any]:
        group_detail = self._find_group_from_cache(group_id) or self._build_fallback_group(group_id)
        detail, client = await self._fetch_group_detail(group_id, preferred_client=self._group_clients.get(group_id))
        if detail:
            group_detail.update(detail)
            if client is not None:
                self._group_clients[group_id] = client

        return group_detail

    async def _hydrate_missing_groups(
        self,
        merged_groups: dict[str, dict[str, Any]],
        group_clients: dict[str, Any],
        group_ids: set[str],
    ) -> None:
        semaphore = asyncio.Semaphore(8)

        async def _load(group_id: str) -> None:
            async with semaphore:
                detail, client = await self._fetch_group_detail(
                    group_id,
                    preferred_client=group_clients.get(group_id),
                )
            if not detail:
                return
            merged_groups[group_id].update(detail)
            if client is not None:
                group_clients[group_id] = client

        await asyncio.gather(*(_load(group_id) for group_id in sorted(group_ids)))

    async def _fetch_group_detail(
        self,
        group_id: str,
        preferred_client: Any | None = None,
    ) -> tuple[dict[str, Any] | None, Any | None]:
        clients = self._build_client_priority_list(preferred_client)
        attempts = [
            (index, client, self._call_action(client, "get_group_info", group_id=int(group_id)))
            for index, client in enumerate(clients)
        ]
        result, client, errors = await self._race_clients(attempts)
        if result is not None and client is not None:
            info = self._extract_object(result)
            if info:
                detail = self._normalize_group_summary(info)
                detail["source"] = "live"
                return detail, client
        if errors:
            logger.debug("Failed to fetch QQ group detail for %s: %s", group_id, "; ".join(errors))
        return None, None

    async def _hydrate_bot_roles(self, force: bool = False) -> None:
        async with self._bot_role_lock:
            groups = [group for group in self._group_list_cache if str(group.get("group_id", "")).strip()]
            if not groups:
                return

            semaphore = asyncio.Semaphore(8)

            async def load_role(group: dict[str, Any]) -> None:
                group_id = str(group.get("group_id", "")).strip()
                if not group_id:
                    return
                if not force and group_id in self._bot_role_cache and (time.time() - self._bot_role_ts.get(group_id, 0)) < self.role_ttl_seconds:
                    return

                async with semaphore:
                    role, client = await self._fetch_bot_role(
                        group_id,
                        preferred_client=self._group_clients.get(group_id),
                    )
                    self._bot_role_cache[group_id] = role
                    self._bot_role_ts[group_id] = time.time()
                    if client is not None:
                        self._group_clients[group_id] = client

            await asyncio.gather(*(load_role(group) for group in groups))
            self._attach_cached_bot_roles(self._group_list_cache)
            self._group_list_cache = self._sort_groups(self._group_list_cache)

    async def _fetch_bot_role(
        self,
        group_id: str,
        preferred_client: Any | None = None,
    ) -> tuple[str, Any | None]:
        clients = self._build_client_priority_list(preferred_client)
        if not clients:
            return "unknown", None
        # bot QQ 并行解析（命中缓存时无额外开销），再对有效账号竞速查询身份
        bot_ids = await asyncio.gather(*(self._get_client_bot_id(client) for client in clients))
        attempts = [
            (
                index,
                client,
                self._call_action(
                    client,
                    "get_group_member_info",
                    group_id=int(group_id),
                    user_id=int(bot_id),
                    no_cache=True,
                ),
            )
            for index, (client, bot_id) in enumerate(zip(clients, bot_ids))
            if bot_id and bot_id.isdigit()
        ]
        result, client, errors = await self._race_clients(attempts)
        if result is not None and client is not None:
            info = self._extract_object(result)
            if info:
                return self._normalize_bot_role(info.get("role")), client
        if errors:
            logger.debug("Failed to fetch bot role for %s: %s", group_id, "; ".join(errors))
        return "unknown", None

    async def _get_client_bot_id(self, client: Any) -> str:
        client_key = id(client)
        cached_bot_id = self._client_bot_ids.get(client_key)
        if cached_bot_id:
            return cached_bot_id

        bot_id = ""
        try:
            result = await self._call_action(client, "get_login_info")
            info = self._extract_object(result)
            bot_id = str(info.get("user_id", "")).strip()
        except Exception:
            try:
                info = await asyncio.wait_for(client.get_login_info(), timeout=API_TIMEOUT_SECONDS) or {}
                bot_id = str(info.get("user_id", "")).strip()
            except Exception as exc:
                logger.debug("Failed to fetch bot self id: %s", exc)

        if bot_id:
            self._client_bot_ids[client_key] = bot_id
        return bot_id

    def _build_client_priority_list(self, preferred_client: Any | None = None) -> list[Any]:
        tried_client_ids: set[int] = set()
        clients: list[Any] = []

        if preferred_client is not None:
            clients.append(preferred_client)
            tried_client_ids.add(id(preferred_client))

        for client in self._iter_clients():
            client_id = id(client)
            if client_id in tried_client_ids:
                continue
            tried_client_ids.add(client_id)
            clients.append(client)

        return clients

    def _attach_cached_bot_roles(self, groups: list[dict[str, Any]]) -> None:
        for group in groups:
            group_id = str(group.get("group_id", "")).strip()
            group["bot_role"] = self._bot_role_cache.get(group_id, "unknown")

    def _iter_clients(self) -> list[Any]:
        clients: list[Any] = []
        for inst in self.context.platform_manager.platform_insts:
            if not isinstance(inst, AiocqhttpAdapter):
                continue
            try:
                client = inst.get_client()
            except Exception:
                continue
            if client is not None:
                clients.append(client)
        return clients

    def _find_group_from_cache(self, group_id: str) -> dict[str, Any] | None:
        for item in self._group_list_cache:
            if item.get("group_id") == group_id:
                return copy.deepcopy(item)
        return None

    @staticmethod
    def _extract_list(result: Any) -> list[dict[str, Any]]:
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
        if isinstance(result, dict):
            data = result.get("data")
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)]
        return []

    @staticmethod
    def _extract_object(result: Any) -> dict[str, Any]:
        if isinstance(result, dict):
            data = result.get("data")
            if isinstance(data, dict):
                return data
            return result
        return {}

    @classmethod
    def _normalize_group_summary(cls, raw_group: dict[str, Any]) -> dict[str, Any]:
        group_id = str(raw_group.get("group_id", "")).strip()
        return {
            "group_id": group_id,
            "group_name": str(raw_group.get("group_name", "")).strip() or f"群 {group_id}",
            "avatar": cls._build_avatar(group_id),
            "member_count": cls._safe_int(raw_group.get("member_count"), 0),
            "max_member_count": cls._safe_int(raw_group.get("max_member_count"), 0),
            "source": "live",
        }

    @staticmethod
    def _needs_detail_refresh(raw_group: dict[str, Any], group_id: str) -> bool:
        return not str(raw_group.get("group_name", "")).strip() or not group_id

    @classmethod
    def _build_fallback_group(cls, group_id: str) -> dict[str, Any]:
        return {
            "group_id": group_id,
            "group_name": f"群 {group_id}",
            "avatar": cls._build_avatar(group_id),
            "member_count": 0,
            "max_member_count": 0,
            "source": "cached",
        }

    @staticmethod
    def _build_avatar(group_id: str) -> str:
        return f"https://p.qlogo.cn/gh/{group_id}/{group_id}/640"

    @staticmethod
    def _safe_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _normalize_bot_role(value: Any) -> str:
        role = str(value or "").strip().lower()
        if role in {"owner", "admin", "member"}:
            return role
        return "unknown"

    @staticmethod
    def _sort_groups(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            groups,
            key=lambda item: (
                BOT_ROLE_PRIORITY.get(str(item.get("bot_role", "unknown")), 2),
                not str(item.get("group_id", "")).isdigit(),
                int(item["group_id"]) if str(item.get("group_id", "")).isdigit() else 0,
                item.get("group_name", ""),
            ),
        )
