"""宵禁持久化：JSON 文件 IO（同步写 + 线程池异步包装）。"""

import json
from pathlib import Path

import anyio
from astrbot.api import logger


class CurfewStore:
    """负责宵禁任务数据的统一持久化"""

    def __init__(self, file: Path):
        self.file = file
        # {"bot_id": {"group_id": {"start_time", "end_time"}}
        self.data: dict[str, dict[str, dict[str, str]]] = {}

    def load(self) -> dict[str, dict]:
        if not self.file.exists():
            return {}
        try:
            with self.file.open("r", encoding="utf-8") as f:
                self.data = json.load(f)
        except Exception as e:
            logger.error(f"加载宵禁任务数据失败: {e}", exc_info=True)
            self.data = {}
        return self.data

    def save(self):
        try:
            with self.file.open("w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            logger.debug("宵禁任务数据已保存")
        except Exception as e:
            logger.error(f"保存宵禁任务数据失败: {e}", exc_info=True)

    async def save_async(self):
        try:
            await anyio.to_thread.run_sync(self.save)
        except Exception as e:
            logger.error(f"异步保存宵禁任务数据失败: {e}", exc_info=True)
