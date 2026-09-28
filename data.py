import asyncio
import copy
import json
from pathlib import Path

import aiosqlite
from astrbot.api import logger

from .config import PluginConfig
from .utils import parse_bool


class QQAdminGlobalList:
    """全局白名单/黑名单，存储为 JSON 文件"""

    _PATHS = {
        "allow": "_allow_path",
        "block": "_block_path",
    }

    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self._allow_path = data_dir / "global_allow.json"
        self._block_path = data_dir / "global_block.json"
        self._allow: list[str] = []
        self._block: list[str] = []
        self._loaded = False

    def load(self) -> None:
        self._allow = self._load_json(self._allow_path)
        self._block = self._load_json(self._block_path)
        self._loaded = True

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def get(self, list_type: str) -> list[str]:
        """获取名单副本，list_type 只能是 allow 或 block。"""
        self._ensure_loaded()
        if list_type not in self._PATHS:
            raise ValueError("list_type must be 'allow' or 'block'")
        return list(getattr(self, f"_{list_type}"))

    def set(self, list_type: str, ids: list[str]) -> list[str]:
        """覆盖指定全局名单并返回清洗后的结果。"""
        self._ensure_loaded()
        if list_type not in self._PATHS:
            raise ValueError("list_type must be 'allow' or 'block'")
        values = list(dict.fromkeys(str(uid).strip() for uid in ids if str(uid).strip()))
        setattr(self, f"_{list_type}", values)
        self._save_json(getattr(self, self._PATHS[list_type]), values)
        return list(values)

    def add(self, list_type: str, uid: str) -> list[str]:
        """向指定全局名单添加用户并返回当前名单。"""
        values = self.get(list_type)
        uid = str(uid).strip()
        if uid and uid not in values:
            values.append(uid)
        return self.set(list_type, values)

    def remove(self, list_type: str, uid: str) -> list[str]:
        """从指定全局名单移除用户并返回当前名单。"""
        uid = str(uid).strip()
        return self.set(list_type, [item for item in self.get(list_type) if item != uid])

    @staticmethod
    def _load_json(path: Path) -> list[str]:
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                return [str(uid) for uid in data] if isinstance(data, list) else []
        except Exception:
            pass
        return []

    @staticmethod
    def _save_json(path: Path, data: list[str]):
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class QQAdminDB:
    """
    群管插件数据库（极简 API + 动态字段 + 自动补齐）
    """

    # ====================== 字段中英文映射 ======================
    FIELD_MAP = {
        "join_switch": "进群审核",
        "join_min_level": "进群等级门槛",
        "join_max_time": "进群尝试次数",
        "join_accept_words": "进群白词",
        "join_reject_words": "进群黑词",
        "join_no_match_msg": "验证为空拒绝",
        "join_no_match_reject": "未中白词拒绝",
        "join_full_reject": "群满自动拒绝",
        "join_full_msg": "群满拒绝文案",
        "join_single_group": "禁止多群加入",
        "join_silent_reasons": "免通知类型",
        "reject_word_block": "命中黑词拉黑",
        "allow_ids": "进群白名单",
        "block_ids": "进群黑名单",
        "join_welcome": "进群欢迎词",
        "join_ban_time": "进群禁言时长",
        "leave_notify": "主动退群通知",
        "leave_block": "主动退群拉黑",
        "builtin_ban": "启用全局禁词",
        "custom_ban_words": "自定义违禁词",
        "word_ban_time": "禁词禁言时长",
        "spamming_ban_time": "刷屏禁言时长",
        "clear_inactive_days": "清理未发言天数",
        "clear_under_level": "清理等级阈值",
        "clear_skip_allow": "清理跳过白名单",
        "use_global_allow": "使用全局白名单",
        "use_global_block": "使用全局黑名单",
    }

    REVERSE_FIELD_MAP = {v: k for k, v in FIELD_MAP.items()}
    FOLLOW_DEFAULT_MARKER = "__follow_default__"

    # ================================================================

    def __init__(self, config: PluginConfig):
        self.db_path = config.db_path

        # 默认字段（动态配置核心）
        self.default_cfg: dict = config.default

        self._conn = None
        self._cache = {}
        self._initialized = False
        self._init_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()

    # ============================== 初始化 ==============================

    async def init(self):
        async with self._init_lock:
            if self._initialized:
                return

            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = await aiosqlite.connect(str(self.db_path))
            self._conn.row_factory = aiosqlite.Row

            await self._conn.execute("""
                CREATE TABLE IF NOT EXISTS groups (
                    group_id TEXT PRIMARY KEY,
                    data TEXT NOT NULL
                );
            """)
            await self._conn.commit()

            # 加载缓存
            async with self._conn.execute("SELECT group_id, data FROM groups;") as cur:
                async for row in cur:
                    try:
                        self._cache[row["group_id"]] = json.loads(row["data"])
                    except Exception:
                        logger.exception("解析 group 数据失败: %s", row["group_id"])

            self._initialized = True
            logger.info("QQAdminDB initialized (%d groups)", len(self._cache))

    async def _save_to_db(self, gid: str, data):
        if not self._conn:
            raise RuntimeError("请先 init()")

        await self._conn.execute(
            """
            INSERT INTO groups(group_id, data)
            VALUES (?, ?)
            ON CONFLICT(group_id) DO UPDATE SET data=excluded.data;
            """,
            (gid, json.dumps(data, ensure_ascii=False)),
        )
        await self._conn.commit()

    def _strip_meta_fields(self, data: dict | None) -> dict | None:
        if data is None:
            return None
        return {key: copy.deepcopy(value) for key, value in data.items() if key != self.FOLLOW_DEFAULT_MARKER}

    def _is_follow_default_data(self, data: dict | None) -> bool:
        if data is None:
            return True

        marker = data.get(self.FOLLOW_DEFAULT_MARKER)
        if marker is False:
            return False

        clean = self._strip_meta_fields(data) or {}
        if not clean:
            return True

        for key, value in clean.items():
            if key not in self.default_cfg:
                return False
            if value != self.default_cfg[key]:
                return False
        return True

    def is_group_follow_default(self, gid: str) -> bool:
        return self._is_follow_default_data(self._cache.get(gid))

    def _build_explicit_group_record(self, data: dict | None = None) -> dict:
        base = copy.deepcopy(data if data is not None else self.default_cfg)
        base[self.FOLLOW_DEFAULT_MARKER] = False
        return base

    def _fill_missing_defaults(self, data: dict) -> bool:
        """补齐顶层配置和权限配置中的新增字段。"""
        changed = False
        for key, default in self.default_cfg.items():
            if key not in data:
                data[key] = copy.deepcopy(default)
                changed = True
        default_perms = self.default_cfg.get("perms", {})
        perms = data.setdefault("perms", {})
        for key, value in default_perms.items():
            if key not in perms:
                perms[key] = copy.deepcopy(value)
                changed = True
        return changed

    # ============================== 基础：确保配置存在 ==============================

    async def ensure_group(self, gid: str):
        """确保存在群配置，若没有则按 default_cfg 初始化"""
        async with self._write_lock:
            if gid not in self._cache or self._is_follow_default_data(self._cache.get(gid)):
                self._cache[gid] = self._build_explicit_group_record(self.get_group_snapshot(gid))
                await self._save_to_db(gid, self._cache[gid])

    def list_group_ids(self) -> list[str]:
        return sorted(
            self._cache.keys(),
            key=lambda gid: (
                not str(gid).isdigit(),
                int(gid) if str(gid).isdigit() else str(gid),
            ),
        )

    def get_group_snapshot(self, gid: str) -> dict:
        raw = self._cache.get(gid)
        if self._is_follow_default_data(raw):
            data = copy.deepcopy(self.default_cfg)
        else:
            data = self._strip_meta_fields(raw) or {}
        self._fill_missing_defaults(data)
        return data

    # ============================== API ==============================

    async def all(self, gid: str) -> dict:
        """
        获取整个配置，并自动补齐 default_cfg 的字段
        """
        if self.is_group_follow_default(gid):
            return self.get_group_snapshot(gid)

        data = self._cache[gid]

        changed = self._fill_missing_defaults(data)

        if data.get(self.FOLLOW_DEFAULT_MARKER) is not False:
            data[self.FOLLOW_DEFAULT_MARKER] = False
            changed = True

        if changed:
            async with self._write_lock:
                await self._save_to_db(gid, data)

        return self.get_group_snapshot(gid)

    async def get(self, gid: str, field: str, default=None):
        """
        读字段，不存在则补齐 default
        """
        if self.is_group_follow_default(gid):
            snapshot = self.get_group_snapshot(gid)
            if field in snapshot:
                return snapshot[field]
            return copy.deepcopy(default)

        data = self._cache[gid]

        if field not in data:
            async with self._write_lock:
                # double-check after acquiring lock
                if field not in data:
                    data[field] = copy.deepcopy(default)
                    await self._save_to_db(gid, data)

        return data[field]

    async def set(self, gid: str, field: str, value):
        """
        写入字段
        """
        await self.ensure_group(gid)
        async with self._write_lock:
            self._cache[gid][field] = value
            await self._save_to_db(gid, self._cache[gid])

    async def replace_group(self, gid: str, data: dict):
        async with self._write_lock:
            self._cache[gid] = self._build_explicit_group_record(data)
            await self._save_to_db(gid, self._cache[gid])

    async def add(self, gid: str, field: str, value):
        """
        列表字段追加（自动创建列表）
        """
        lst = list(await self.get(gid, field, []))
        if value not in lst:
            lst.append(value)
            await self.set(gid, field, lst)

    async def remove(self, gid: str, field: str, value):
        """
        列表字段删除（自动创建列表）
        """
        lst = [i for i in await self.get(gid, field, []) if i != value]
        await self.set(gid, field, lst)

    # ============================== 删除群配置 ==============================

    async def delete_group(self, gid: str):
        """彻底删除群配置"""
        async with self._write_lock:
            if self._conn:
                await self._conn.execute("DELETE FROM groups WHERE group_id = ?", (gid,))
                await self._conn.commit()
            self._cache.pop(gid, None)

    # ============================== 关闭 ==============================

    async def close(self):
        if self._conn:
            await self._conn.close()
            self._conn = None
            self._initialized = False

    # ====================== 中文展示、读回 ======================

    async def export_cn_lines(self, gid: str) -> str:
        """
        以中文键名 + 多行文本形式输出群配置。
        - 列表字段：用空格分隔
        - 布尔：开 / 关
        - 其它类型按原样输出
        """
        data = await self.all(gid)
        lines = []

        for eng_key, value in data.items():
            cn_key = self.FIELD_MAP.get(eng_key, eng_key)

            # 列表字段 => 用空格分隔
            if isinstance(value, list):
                val_str = " ".join(map(str, value))

            # 布尔字段 => 显示 为“开 / 关”
            elif isinstance(value, bool):
                val_str = "开" if value else "关"

            # 其他字段 => 按原样
            else:
                val_str = str(value)

            lines.append(f"{cn_key}: {val_str}")

        return "\n".join(lines)

    async def import_cn_lines(self, gid: str, text: str) -> dict:
        """
        解析用户提交的中文多行文本并写回 DB
        - 列表字段：空格分隔
        - 布尔：开/关/开启/on/off/true/false/1/0
        - 数字：自动转 int
        - 字符串：原样保存
        """
        await self.ensure_group(gid)
        data = self._cache[gid]

        for line in text.splitlines():
            if ":" not in line:
                continue

            cn_key, raw_v = line.split(":", 1)
            cn_key = cn_key.strip()
            raw_v = raw_v.strip()

            eng_key = self.REVERSE_FIELD_MAP.get(cn_key)
            if not eng_key:
                continue

            old_val = data.get(eng_key)

            # bool 必须优先且独占分支，避免 bool 误入 int 分支
            if isinstance(old_val, bool):
                parsed = parse_bool(raw_v)
                if parsed is not None:
                    data[eng_key] = parsed
                # 解析失败则保留原值，避免误写
                continue
            elif isinstance(old_val, list):
                value = [x for x in raw_v.split() if x]
            elif isinstance(old_val, int):
                try:
                    value = int(raw_v)
                except ValueError:
                    value = old_val  # 防错保底
            else:
                value = raw_v

            data[eng_key] = value

        async with self._write_lock:
            await self._save_to_db(gid, data)
        return self.get_group_snapshot(gid)

    async def follow_default(self, gid: str | None = None):
        """让指定群（或全部群）重新跟随默认群配置"""
        async with self._write_lock:
            if gid is None:
                if self._conn:
                    await self._conn.execute("DELETE FROM groups")
                    await self._conn.commit()
                self._cache.clear()
                logger.info("所有群聊的群管配置已重新跟随默认值")
                return

            normalized_gid = str(gid)
            if self._conn:
                await self._conn.execute(
                    "DELETE FROM groups WHERE group_id = ?",
                    (normalized_gid,),
                )
                await self._conn.commit()
            self._cache.pop(normalized_gid, None)
        logger.info(f"群聊{normalized_gid}的群管配置已重新跟随默认值")

    async def reset_to_default(self, gid: str | None = None):
        """把指定群（或全部群）配置恢复成 default_cfg"""
        await self.follow_default(gid)
