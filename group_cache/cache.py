"""QQ 群信息缓存：多 aiocqhttp 连接聚合，带 TTL 与 DB 兜底。

刷新管线（_refresh_group_list）：
1) _load_live_groups：多 client 并行 get_group_list 合并去重。
2) _add_db_fallbacks：实时拿不到的已知群用 DB 兜底（source=cached）。
3) _hydrate_missing_groups：缺详情（无名/人数全 0）限流补 get_group_info。
4) Bot 身份（bot_role）单独懒补 _hydrate_bot_roles，TTL 600s。
读路径：list_groups（列表）/ get_group（单群详情）/ list_groups_with_bot_roles；
并发原语：_race_clients 多连接竞速首个有效结果，_gather_limited 限流 8。
失效判定唯一出口 is_stale_group：live 或人数>0 即存活，否则计票，
连续 STALE_VOTES_REQUIRED 次才判失效，避免单次抖动误删。
"""

import asyncio
import copy
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.star import Context
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (
    AiocqhttpAdapter,
)

from ..data import QQAdminDB
from .utils import (
    build_fallback_group,
    extract_list,
    extract_object,
    format_error,
    needs_detail_refresh,
    normalize_bot_role,
    normalize_group_summary,
    sort_groups,
)

# OneBot 单次调用超时封顶（秒）：防实现端无响应时无限挂起，与跨群探测的超时策略一致
API_TIMEOUT_SECONDS = 10.0
# 优先 client 起跑窗口（秒）：窗口内它成功则直接采用；否则其余跟上一起竞速
PREFERRED_HEAD_START_SECONDS = 0.5
# 详情/身份批量补齐的最大并发数
HYDRATE_CONCURRENCY = 8
# 连续判定失效多少次才清理，避免单次 API 抖动误删
STALE_VOTES_REQUIRED = 2


class QQGroupInfoCache:
    """QQ 群信息缓存：聚合多 aiocqhttp 连接的群列表/详情/Bot 身份，带 TTL 与 DB 兜底。"""

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
        # 失效投票计数：单次 API 抖动不删库，连续多次判定失效才清理
        self._stale_votes: dict[str, int] = {}

    # ---------- 对外接口 ----------

    async def list_groups(self, force: bool = False, with_details: bool = False) -> list[dict[str, Any]]:
        await self._ensure_list_cache(force=force, with_details=with_details)
        return copy.deepcopy(self._group_list_cache)

    async def list_groups_with_bot_roles(
        self,
        force_bot_roles: bool = False,
    ) -> list[dict[str, Any]]:
        await self._ensure_list_cache()
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
        self.invalidate(normalized_group_id)
        self._group_clients.pop(normalized_group_id, None)
        self._bot_role_cache.pop(normalized_group_id, None)
        self._bot_role_ts.pop(normalized_group_id, None)

    @property
    def last_refresh_error(self) -> str | None:
        """最近一次群列表刷新的错误（无错误返回 None），供 Web 面板展示。"""
        return self._last_refresh_error

    def is_stale_group(self, group_info: dict[str, Any]) -> bool:
        """群存活判定：live 来源或人数有效即存活；否则计票，连续失效达标才判 stale。

        存活策略的唯一 owner（判定 + 计票 + 删除都在本模块）。
        "__default__" 字面量对应 page_service.DEFAULT_GROUP_ID（避免循环导入而未引用），
        该条目不会进入缓存路径，此处仅作防御。
        """
        group_id = str(group_info.get("group_id", "")).strip()
        if not group_id or group_id == "__default__":
            return False
        # live 来源或人数有效 → 存活，清除投票
        if group_info.get("source") == "live":
            self._stale_votes.pop(group_id, None)
            return False
        try:
            member_count = int(group_info.get("member_count", 0))
        except (TypeError, ValueError):
            member_count = 0
        if member_count > 0:
            self._stale_votes.pop(group_id, None)
            return False
        votes = self._stale_votes.get(group_id, 0) + 1
        if votes >= STALE_VOTES_REQUIRED:
            self._stale_votes.pop(group_id, None)
            return True
        self._stale_votes[group_id] = votes
        return False

    async def delete_group_data(self, group_id: str | int | None) -> str:
        """删库 + 清缓存，返回归一化群号；非数字群号抛 ValueError。"""
        gid = str(group_id or "").strip()
        if not gid or not gid.isdigit():
            raise ValueError("group_id must be a numeric string")
        await self.db.delete_group(gid)
        self.remove_group(gid)
        return gid

    # ---------- 列表刷新管线 ----------

    async def _ensure_list_cache(self, force: bool = False, with_details: bool = False) -> None:
        if force or with_details or not self._is_fresh() or not self._group_list_cache:
            await self._refresh_group_list(force=force, with_details=with_details)

    def _is_fresh(self) -> bool:
        return (time.time() - self._last_refresh_at) < self.ttl_seconds

    async def _refresh_group_list(self, force: bool = False, with_details: bool = False) -> None:
        async with self._lock:
            if not force and not with_details and self._is_fresh() and self._group_list_cache:
                return

            clients = self._iter_clients()
            if not clients:
                logger.warning("No aiocqhttp client available, skip QQ group list refresh and use cached groups only")

            merged_groups, group_clients, missing_detail_group_ids, refresh_errors = await self._load_live_groups(clients)
            self._add_db_fallbacks(merged_groups, missing_detail_group_ids)

            if with_details:
                # 手动同步要求数据最新：列表接口的人数可能是缓存值，用实时详情全量校准
                missing_detail_group_ids.update(group_id for group_id, group in merged_groups.items() if group.get("source") == "live")

            if missing_detail_group_ids:
                await self._hydrate_missing_groups(merged_groups, group_clients, missing_detail_group_ids)

            self._record_refresh_error(bool(clients), merged_groups, refresh_errors)

            groups = list(merged_groups.values())
            self._attach_cached_bot_roles(groups)
            self._group_list_cache = sort_groups(groups)
            self._group_clients = group_clients
            # 整表已刷新，逐群详情缓存即视为过期，避免右侧面板读到旧人数
            self.invalidate()
            self._last_refresh_at = time.time()

    async def _load_live_groups(
        self,
        clients: list[Any],
    ) -> tuple[dict[str, dict[str, Any]], dict[str, Any], set[str], list[str]]:
        """多 client 并行拉取群列表并合并；总耗时取最慢者（仍受单次超时封顶），而非顺序累加。"""
        merged_groups: dict[str, dict[str, Any]] = {}
        group_clients: dict[str, Any] = {}
        missing_detail_group_ids: set[str] = set()
        refresh_errors: list[str] = []

        loaded = await asyncio.gather(*(self._fetch_group_list(index, client) for index, client in enumerate(clients)))
        for client, items, error in loaded:
            if error is not None:
                refresh_errors.append(error)
                continue
            for item in items:
                group_id = str(item.get("group_id", "")).strip()
                if not group_id or group_id in merged_groups:
                    continue
                merged_groups[group_id] = normalize_group_summary(item)
                group_clients[group_id] = client
                if needs_detail_refresh(item, group_id):
                    missing_detail_group_ids.add(group_id)
        return merged_groups, group_clients, missing_detail_group_ids, refresh_errors

    def _add_db_fallbacks(
        self,
        merged_groups: dict[str, dict[str, Any]],
        missing_detail_group_ids: set[str],
    ) -> None:
        """实时接口拿不到的已知群，用 DB 兜底条目补齐并标记待补详情。"""
        for group_id in self.db.list_group_ids():
            if group_id not in merged_groups:
                merged_groups[group_id] = build_fallback_group(group_id)
                missing_detail_group_ids.add(group_id)

    def _record_refresh_error(
        self,
        has_clients: bool,
        merged_groups: dict[str, dict[str, Any]],
        refresh_errors: list[str],
    ) -> None:
        """记录本次刷新的面板可见错误：拿到实时数据即视为成功并清零。"""
        live_count = sum(1 for group in merged_groups.values() if group.get("source") == "live")
        if not has_clients:
            self._last_refresh_error = "群列表刷新失败：无可用的 aiocqhttp 连接，请检查机器人是否在线"
        elif live_count == 0 and refresh_errors:
            detail = "; ".join(refresh_errors)
            self._last_refresh_error = f"群列表刷新失败：{detail[:500]}"
        else:
            self._last_refresh_error = None

    # ---------- 抓取层 ----------

    async def _fetch_group_list(self, index: int, client: Any) -> tuple[Any, list[dict[str, Any]], str | None]:
        """单个 client 拉取群列表，返回 (client, 群条目, 错误信息)。"""
        label = self._describe_client(index, client)
        try:
            result = await self._call_action(client, "get_group_list")
        except Exception as exc:
            formatted = format_error(exc)
            logger.warning("Failed to load QQ group list via %s: %s", label, formatted)
            return client, [], f"{label}: {formatted}"
        items = extract_list(result)
        if not items and not (isinstance(result, list) or (isinstance(result, dict) and isinstance(result.get("data"), list))):
            logger.warning("QQ group list via %s returned unexpected result: %s", label, repr(result)[:500])
            return client, [], f"{label}：接口返回异常"
        return client, items, None

    async def _load_group_detail(self, group_id: str) -> dict[str, Any]:
        group_detail = self._find_group_from_cache(group_id) or build_fallback_group(group_id)
        detail, client = await self._fetch_group_detail(group_id, preferred_client=self._group_clients.get(group_id))
        if detail:
            group_detail.update(detail)
            if client is not None:
                self._group_clients[group_id] = client

        return group_detail

    async def refresh_details_only(self) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        """只为当前缓存中的群补实时详情（不重拉整表、不做失效计票/删除）；返回 (更新后的列表, 失败群条目)。"""
        async with self._lock:
            cached = [group for group in self._group_list_cache if str(group.get("group_id", "")).strip()]
            if not cached:
                return [], []
            merged_groups = {str(group["group_id"]): dict(group) for group in cached}
            group_clients = {group_id: self._group_clients.get(group_id) for group_id in merged_groups}
            failed_ids = await self._hydrate_missing_groups(merged_groups, group_clients, set(merged_groups))
            self._attach_cached_bot_roles(list(merged_groups.values()))
            self._group_list_cache = sort_groups(list(merged_groups.values()))
            for group_id, client in group_clients.items():
                if client is not None:
                    self._group_clients[group_id] = client
            failed = [{"group_id": group_id, "group_name": merged_groups[group_id].get("group_name") or group_id} for group_id in failed_ids if group_id in merged_groups]
            return list(merged_groups.values()), failed

    async def _hydrate_missing_groups(
        self,
        merged_groups: dict[str, dict[str, Any]],
        group_clients: dict[str, Any],
        group_ids: set[str],
    ) -> list[str]:
        """批量补详情，返回失败群号（调用方可忽略）。"""
        failed_ids: list[str] = []

        async def _load(group_id: str) -> None:
            detail, client = await self._fetch_group_detail(
                group_id,
                preferred_client=group_clients.get(group_id),
            )
            if not detail:
                failed_ids.append(group_id)
                return
            merged_groups[group_id].update(detail)
            if client is not None:
                group_clients[group_id] = client

        await self._gather_limited(_load(group_id) for group_id in sorted(group_ids))
        return sorted(failed_ids)

    async def _fetch_first_valid(
        self,
        group_id: str,
        action: str,
        attempts: list[tuple[int, Any, Any]],
        prefer_first: bool,
        parse: Any,
        fallback: Any,
    ) -> tuple[Any | None, Any | None]:
        """竞速取首个有效结果；parse(info) 返回 None 视为无效继续等；全无效返回 (fallback, None)。"""
        result, client, errors = await self._race_clients(
            attempts,
            prefer_first=prefer_first,
            valid=lambda raw: parse(extract_object(raw)) is not None,
        )
        if result is None or client is None:
            if errors:
                logger.debug("Failed to %s for %s: %s", action, group_id, "; ".join(errors))
            return fallback, None
        return parse(extract_object(result)), client

    def _parse_group_detail(self, info: dict[str, Any]) -> dict[str, Any] | None:
        if not info:
            return None
        detail = normalize_group_summary(info)
        detail["source"] = "live"
        return detail

    def _parse_bot_role(self, info: dict[str, Any]) -> str | None:
        return normalize_bot_role(info.get("role")) if info else None

    async def _fetch_group_detail(
        self,
        group_id: str,
        preferred_client: Any | None = None,
    ) -> tuple[dict[str, Any] | None, Any | None]:
        clients = self._build_client_priority_list(preferred_client)
        attempts = [(index, client, self._call_action(client, "get_group_info", group_id=int(group_id))) for index, client in enumerate(clients)]
        return await self._fetch_first_valid(
            group_id,
            "fetch QQ group detail",
            attempts,
            prefer_first=preferred_client is not None,
            parse=self._parse_group_detail,
            fallback=None,
        )

    async def _hydrate_bot_roles(self, force: bool = False) -> None:
        async with self._bot_role_lock:
            groups = [group for group in self._group_list_cache if str(group.get("group_id", "")).strip()]
            if not groups:
                return

            async def load_role(group: dict[str, Any]) -> None:
                group_id = str(group.get("group_id", "")).strip()
                if not group_id:
                    return
                if not force and group_id in self._bot_role_cache and (time.time() - self._bot_role_ts.get(group_id, 0)) < self.role_ttl_seconds:
                    return

                role, client = await self._fetch_bot_role(
                    group_id,
                    preferred_client=self._group_clients.get(group_id),
                )
                self._bot_role_cache[group_id] = role
                self._bot_role_ts[group_id] = time.time()
                if client is not None:
                    self._group_clients[group_id] = client

            await self._gather_limited(load_role(group) for group in groups)
            self._attach_cached_bot_roles(self._group_list_cache)
            self._group_list_cache = sort_groups(self._group_list_cache)

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
        return await self._fetch_first_valid(
            group_id,
            "fetch bot role",
            attempts,
            prefer_first=preferred_client is not None,
            parse=self._parse_bot_role,
            fallback="unknown",
        )

    async def _get_client_bot_id(self, client: Any) -> str:
        client_key = id(client)
        cached_bot_id = self._client_bot_ids.get(client_key)
        if cached_bot_id:
            return cached_bot_id

        bot_id = ""
        try:
            result = await self._call_action(client, "get_login_info")
            info = extract_object(result)
            bot_id = str(info.get("user_id", "")).strip()
        except Exception:
            try:
                info = await self._call_with_timeout(client.get_login_info()) or {}
                bot_id = str(info.get("user_id", "")).strip()
            except Exception as exc:
                logger.debug("Failed to fetch bot self id: %s", exc)

        if bot_id:
            self._client_bot_ids[client_key] = bot_id
        return bot_id

    # ---------- 并发原语 ----------

    @staticmethod
    async def _call_with_timeout(coro: Any) -> Any:
        """给 OneBot 调用加超时封顶，超时抛 TimeoutError 由调用处按失败处理。"""
        return await asyncio.wait_for(coro, timeout=API_TIMEOUT_SECONDS)

    @classmethod
    async def _call_action(cls, client: Any, action: str, **kwargs: Any) -> Any:
        return await cls._call_with_timeout(client.call_action(action, **kwargs))

    @staticmethod
    async def _gather_limited(coroutines: Any, limit: int = HYDRATE_CONCURRENCY) -> list[Any]:
        """限流并发执行一组协程，结果顺序与输入一致。"""
        semaphore = asyncio.Semaphore(limit)

        async def _run(coro: Any) -> Any:
            async with semaphore:
                return await coro

        return await asyncio.gather(*(_run(coro) for coro in coroutines))

    async def _race_clients(
        self,
        attempts: list[tuple[int, Any, Any]],
        prefer_first: bool = False,
        valid: Any = None,
    ) -> tuple[Any | None, Any | None, list[str]]:
        """多 client 并行竞速，返回 (首个成功结果, 对应 client, 失败记录)。
        prefer_first 为真时给首个（优先）client 一个起跑窗口：窗口内它成功则直接采用；
        否则其余跟上一起竞速，仍取首个成功者。
        valid 为空结果判定：未通过的结果不算成功，继续等其他 client（也不记为错误）。"""
        if not attempts:
            return None, None, []
        if len(attempts) == 1 or not prefer_first:
            return await self._race_all(attempts, valid)
        index, client, coro = attempts[0]
        preferred_task = asyncio.ensure_future(coro)
        done, _ = await asyncio.wait({preferred_task}, timeout=PREFERRED_HEAD_START_SECONDS)
        if not done:
            # 起跑窗口内优先端未返回：连同它一起竞速（已是 Task，ensure_future 会直接复用）
            return await self._race_all([(index, client, preferred_task), *attempts[1:]], valid)
        try:
            result = preferred_task.result()
        except Exception as exc:
            preferred_error = f"{self._describe_client(index, client)}: {format_error(exc)}"
            result, client, errors = await self._race_all(attempts[1:], valid)
            errors.insert(0, preferred_error)
            return result, client, errors
        if valid is None or valid(result):
            return result, client, []
        # 优先端结果无效：其余竞速（优先端不再参与）
        return await self._race_all(attempts[1:], valid)

    async def _race_all(
        self,
        attempts: list[tuple[int, Any, Any]],
        valid: Any = None,
    ) -> tuple[Any | None, Any | None, list[str]]:
        """纯竞速：首个成功者胜出；全部失败返回 (None, None, errors)。"""
        errors: list[str] = []
        if not attempts:
            return None, None, errors
        tasks = {asyncio.ensure_future(coro): (index, client) for index, client, coro in attempts}
        pending = set(tasks)
        try:
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    index, client = tasks[task]
                    try:
                        result = task.result()
                    except Exception as exc:
                        errors.append(f"{self._describe_client(index, client)}: {format_error(exc)}")
                        continue
                    if valid is not None and not valid(result):
                        continue
                    return result, client, errors
        finally:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        return None, None, errors

    # ---------- 连接发现 ----------

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

    # ---------- 实例相关小工具（纯数据变换见 .utils，可单测） ----------

    def _describe_client(self, index: int, client: Any) -> str:
        """给 client 一个可读标识（优先用已缓存的 bot QQ，便于多开时定位）。"""
        bot_id = self._client_bot_ids.get(id(client))
        return f"client#{index}(bot {bot_id})" if bot_id else f"client#{index}"

    def _attach_cached_bot_roles(self, groups: list[dict[str, Any]]) -> None:
        for group in groups:
            group_id = str(group.get("group_id", "")).strip()
            group["bot_role"] = self._bot_role_cache.get(group_id, "unknown")

    def _find_group_from_cache(self, group_id: str) -> dict[str, Any] | None:
        for item in self._group_list_cache:
            if item.get("group_id") == group_id:
                return copy.deepcopy(item)
        return None
