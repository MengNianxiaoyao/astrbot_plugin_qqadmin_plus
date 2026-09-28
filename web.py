from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast

from astrbot.api import logger
from astrbot.api.star import Context

try:
    from quart import jsonify as quart_jsonify
    from quart import request as quart_request_obj
except ImportError:
    quart_jsonify = None
    quart_request_obj = None

from .config import PluginConfig
from .core.banpro_handle import BanproHandle
from .data import QQAdminDB, QQAdminGlobalList
from .group_info_cache import QQGroupInfoCache
from .page_service import QQAdminPageService

PLUGIN_NAME = "astrbot_plugin_qqadmin_plus"


class QQAdminWebController:
    """前端面板路由；鉴权由 AstrBot 框架层统一处理（/api/* 需携带 Dashboard Token），本插件不再额外校验。"""

    def __init__(
        self,
        context: Context,
        cfg: PluginConfig,
        db: QQAdminDB,
        group_cache: QQGroupInfoCache,
        global_list: QQAdminGlobalList,
        banpro: BanproHandle,
    ):
        self.context = context
        self.service = QQAdminPageService(cfg, db, group_cache, global_list, banpro)

    def register_routes(self) -> None:
        routes = [
            (   "/ping",
                self.page_ping,
                ["GET"],
                "Page ping",
            ),
            (
                "/settings/bootstrap",
                self.page_bootstrap,
                ["GET"],
                "Load settings page bootstrap data",
            ),
            (
                "/settings/groups/refresh",
                self.page_refresh_groups,
                ["POST"],
                "Refresh QQ group list",
            ),
            (
                "/settings/groups/roles",
                self.page_refresh_group_roles,
                ["POST"],
                "Load bot roles for QQ groups",
            ),
            (   "/settings/group",
                self.page_get_group,
                ["GET"],
                "Load one group config",
            ),
            (
                "/settings/group",
                self.page_update_group,
                ["POST"],
                "Update one group config",
            ),
            (
                "/settings/group/reset",
                self.page_reset_group,
                ["POST"],
                "Reset one group config",
            ),
            (
                "/settings/global-list",
                self.page_get_global_lists,
                ["GET"],
                "Get global allow/block lists",
            ),
            (
                "/settings/global-list",
                self.page_update_global_list,
                ["POST"],
                "Update global allow/block list",
            ),
            (
                "/settings/global-ban-words",
                self.page_get_global_ban_words,
                ["GET"],
                "Get global and available builtin ban words",
            ),
            (
                "/settings/global-ban-words",
                self.page_update_global_ban_words,
                ["POST"],
                "Update global ban words",
            ),
            (
                "/settings/global-ban-words/import",
                self.page_import_builtin_ban_words,
                ["POST"],
                "Import builtin ban words into global ban words",
            ),
            (
                "/settings/global-ban-words/restore",
                self.page_restore_builtin_ban_words,
                ["POST"],
                "Restore global ban words from builtin ban words",
            ),
        ]
        for path, handler, methods, desc in routes:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}{path}",
                self._wrap_handler(handler),
                methods,
                desc,
            )

    @staticmethod
    def _check_quart_available() -> None:
        if quart_jsonify is None or quart_request_obj is None:
            raise RuntimeError("Web framework is unavailable")

    @staticmethod
    def _jsonify(payload: dict[str, Any]):
        QQAdminWebController._check_quart_available()
        return cast(Callable[[dict[str, Any]], Any], quart_jsonify)(payload)

    @staticmethod
    def _request():
        QQAdminWebController._check_quart_available()
        return cast(Any, quart_request_obj)

    def _wrap_handler(self, handler: Callable[[], Awaitable]) -> Callable[[], Awaitable]:
        async def wrapped():
            self._check_quart_available()
            try:
                return await handler()
            except ValueError as exc:
                return self._jsonify({"ok": False, "message": str(exc)}), 400
            except Exception as exc:
                logger.exception("QQAdmin page request failed")
                return self._jsonify({"ok": False, "message": str(exc)}), 500

        wrapped.__name__ = handler.__name__
        return wrapped

    async def page_ping(self):
        return self._jsonify({"ok": True, "message": "pong"})

    async def page_bootstrap(self):
        return self._jsonify({"ok": True, "data": await self.service.get_bootstrap_payload()})

    async def page_refresh_groups(self):
        return self._jsonify({"ok": True, "data": await self.service.list_groups(force=True)})

    async def page_refresh_group_roles(self):
        payload = await self._request().get_json(force=True, silent=True) or {}
        force = str(payload.get("force", "")).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        return self._jsonify({"ok": True, "data": await self.service.list_groups_with_bot_roles(force)})

    async def page_get_group(self):
        request = self._request()
        group_id = request.args.get("group_id", "")
        force = request.args.get("force", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        return self._jsonify(
            {
                "ok": True,
                "data": await self.service.get_group_config(group_id, force=force),
            }
        )

    async def page_update_group(self):
        payload = await self._request().get_json(force=True, silent=True) or {}
        group_id = payload.get("group_id")
        config = payload.get("config")
        result = await self.service.update_group_config(group_id, config)
        return self._jsonify({"ok": True, "message": "Group config saved", "data": result})

    async def page_reset_group(self):
        payload = await self._request().get_json(force=True, silent=True) or {}
        group_id = payload.get("group_id")
        result = await self.service.reset_group_config(group_id)
        return self._jsonify({"ok": True, "message": "Group config reset", "data": result})

    async def page_get_global_lists(self):
        data = await self.service.get_global_lists()
        return self._jsonify({"ok": True, "data": data})

    async def page_update_global_list(self):
        payload = await self._request().get_json(force=True, silent=True) or {}
        list_type = payload.get("type", "")
        items = payload.get("items", [])
        result = await self.service.update_global_list(list_type, items)
        return self._jsonify({"ok": True, "message": f"全局{'白名单' if list_type == 'allow' else '黑名单'}已更新", "data": result})

    async def page_get_global_ban_words(self):
        return self._jsonify({"ok": True, "data": await self.service.get_global_ban_words()})

    async def page_update_global_ban_words(self):
        payload = await self._request().get_json(force=True, silent=True) or {}
        words = await self.service.update_global_ban_words(payload.get("words", []))
        return self._jsonify({"ok": True, "message": "全局禁词已更新", "data": words})

    async def page_import_builtin_ban_words(self):
        payload = await self._request().get_json(force=True, silent=True) or {}
        words = await self.service.import_builtin_ban_words(payload.get("words", []))
        return self._jsonify({"ok": True, "message": "内置禁词已导入", "data": words})

    async def page_restore_builtin_ban_words(self):
        words = await self.service.restore_builtin_ban_words()
        return self._jsonify({"ok": True, "message": "已恢复内置禁词", "data": words})
