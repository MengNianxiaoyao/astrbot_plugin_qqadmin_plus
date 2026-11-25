"""全局白名单/黑名单：JSON 文件存储，内存缓存 + 懒加载。"""

import asyncio
from pathlib import Path

from astrbot.core.utils.string_utils import normalize_and_dedupe_strings

from ..utils import load_json, save_json_atomic


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
        self._lock = asyncio.Lock()

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

    async def set(self, list_type: str, ids: list[str]) -> list[str]:
        """覆盖指定全局名单并返回清洗后的结果（写操作串行，避免面板与命令互相覆盖）。"""
        async with self._lock:
            return self._set_locked(list_type, ids)

    def _set_locked(self, list_type: str, ids: list[str]) -> list[str]:
        self._ensure_loaded()
        if list_type not in self._PATHS:
            raise ValueError("list_type must be 'allow' or 'block'")
        values = normalize_and_dedupe_strings(str(uid) for uid in ids)
        setattr(self, f"_{list_type}", values)
        self._save_json(getattr(self, self._PATHS[list_type]), values)
        return list(values)

    async def add(self, list_type: str, uid: str) -> list[str]:
        """向指定全局名单添加用户并返回当前名单。"""
        async with self._lock:
            values = self.get(list_type)
            uid = str(uid).strip()
            if uid and uid not in values:
                values.append(uid)
            return self._set_locked(list_type, values)

    @staticmethod
    def _load_json(path: Path) -> list[str]:
        data = load_json(path, [])
        return [str(uid) for uid in data] if isinstance(data, list) else []

    @staticmethod
    def _save_json(path: Path, data: list[str]):
        save_json_atomic(path, data, indent=None)
