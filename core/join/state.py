import json
import time
from collections.abc import Callable
from pathlib import Path

# 跨群申请记录有效期（秒）：QQ 进群申请过期后自动失效，不再阻拦其他群申请
APPLIED_TTL = 7 * 86400
# 待审批默认新鲜期（天）：各群可用 join_pending_ttl 覆盖；计数/对账只认新鲜记录
PENDING_DEFAULT_DAYS = 3
PENDING_TTL = PENDING_DEFAULT_DAYS * 86400
# 待审批登记持久化上限（天）：落盘恢复与兜底清理的宽口径，只防无限增长，不参与业务判定
PENDING_MAX_DAYS = 30
PENDING_MAX_TTL = PENDING_MAX_DAYS * 86400


def pending_ttl_secs(value: object) -> float:
    """新鲜期换算（天→秒）：非法/过小回退默认天数。判定与面板共用。"""
    if isinstance(value, bool):
        return PENDING_DEFAULT_DAYS * 86400.0
    try:
        return max(1, int(value)) * 86400.0  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return PENDING_DEFAULT_DAYS * 86400.0


# 待审批登记落盘文件名：重启后恢复，避免未决申请丢失
PENDING_FILE = "join_pending.json"


class JoinState:
    """进群模块的运行时状态：失败计数、待审批登记、跨群申请记录。"""

    def __init__(self, data_dir: Path | None = None):
        # 进群尝试计数：{群_QQ: 次数}，带 24h 过期
        self.fail: dict[str, int] = {}
        self.fail_time: dict[str, float] = {}
        # 待人工审批的进群申请：{通知消息ID: {flag, gid, uid, nickname, ts}}，落盘重启可恢复
        self.pending: dict[str, dict] = {}
        self._pending_path = (data_dir / PENDING_FILE) if data_dir is not None else None
        if self._pending_path is not None:
            self._load_pending()
        # 跨群申请记录：{uid: {gid: {flag, ts, name}}}，用于多群同时申请判定
        self.applied: dict[str, dict[str, dict]] = {}

    def fail_count(self, key: str, now: float | None = None) -> int:
        """读取失败计数（含 24h 过期语义，只读不 mutation）。"""
        now = time.time() if now is None else now
        if key in self.fail_time and now - self.fail_time[key] > 86400:
            return 0
        return self.fail.get(key, 0)

    def record_fail(self, key: str, now: float | None = None) -> int:
        """失败计数 +1（含 24h 过期语义），返回累加后的次数。"""
        now = time.time() if now is None else now
        if key in self.fail_time and now - self.fail_time[key] > 86400:
            self.fail.pop(key, None)
        self.fail[key] = self.fail.get(key, 0) + 1
        self.fail_time[key] = now
        return self.fail[key]

    def clear_fail(self, key: str):
        """清零指定键的失败计数。"""
        self.fail.pop(key, None)
        self.fail_time.pop(key, None)

    def fresh_entries(self, gid: str, now: float | None = None, ttl: float = PENDING_TTL) -> list[tuple[str, dict]]:
        """本群新鲜待审批登记 [(通知ID, 登记)]：计数取 len，对账取 uid，调用方各取所需。"""
        now = time.time() if now is None else now
        return [(k, r) for k, r in self.pending.items() if r.get("gid") == gid and now - r.get("ts", 0) < ttl]

    def drop_pending(self, match: Callable[[str, dict], bool]) -> int:
        """按谓词批量清除登记并落盘，返回清除条数。"""
        keys = [k for k, v in self.pending.items() if match(k, v)]
        for k in keys:
            self.pending.pop(k, None)
        if keys:
            self._save_pending()
        return len(keys)

    def _drop_stale(self, now: float) -> None:
        """清理远超上限的登记，防止内存无限增长（用宽口径，各群新鲜期只在计数时收紧）。"""
        threshold = now - PENDING_MAX_TTL
        self.drop_pending(lambda k, r: r.get("ts", 0) < threshold)

    def reload_pending(self) -> None:
        """从落盘文件重读登记（面板“刷新”用：直接改 json 文件后无需重启即生效）。
        写透不变式保证落盘是全集；文件损坏时保持内存不动。"""
        self._load_pending()

    def _load_pending(self) -> None:
        """重启恢复：只认新鲜且结构完整的登记，过期/坏数据直接丢弃。"""
        if self._pending_path is None:
            return
        try:
            if not self._pending_path.exists():
                return
            data = json.loads(self._pending_path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(data, dict):
            return
        now = time.time()
        self.pending = {
            str(k): v
            for k, v in data.items()
            if isinstance(v, dict)
            and isinstance(v.get("gid"), str)
            and isinstance(v.get("uid"), str)
            and isinstance(v.get("flag"), str)
            and (v.get("platform_id") is None or isinstance(v.get("platform_id"), str))
            and (v.get("comment") is None or isinstance(v.get("comment"), str))
            and isinstance(v.get("ts"), (int, float))
            and now - v["ts"] < PENDING_MAX_TTL
        }

    def _save_pending(self) -> None:
        """写透落盘；IO 失败不影响审核主流程。"""
        if self._pending_path is None:
            return
        try:
            self._pending_path.parent.mkdir(parents=True, exist_ok=True)
            self._pending_path.write_text(json.dumps(self.pending, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def track_pending(
        self,
        key: str,
        gid: str,
        uid: str,
        nickname: str,
        flag: str,
        platform_id: str | None = None,
        comment: str | None = None,
    ):
        """登记一条待人工审批的进群申请，供管理员回复审批时精确匹配；platform_id 用于面板直接批驳时定位 bot。"""
        self.pending[key] = {
            "gid": gid,
            "uid": uid,
            "nickname": nickname,
            "flag": flag,
            "platform_id": platform_id,
            "comment": (str(comment).strip()[:500] if comment else ""),
            "ts": time.time(),
        }
        # 简单清理过期记录，防止内存无限增长
        if len(self.pending) > 200:
            self._drop_stale(time.time())
        self._save_pending()

    def register_application(self, uid: str, gid: str, flag: str, name: str):
        """登记一次进群申请，并清理过期记录。"""
        now = time.time()
        expiry = now - APPLIED_TTL
        for u in list(self.applied):
            groups = self.applied[u]
            for g in [g for g, r in groups.items() if r.get("ts", 0) < expiry]:
                groups.pop(g, None)
            if not groups:
                self.applied.pop(u, None)
        # 简单限流，防止内存无限增长
        total = sum(len(groups) for groups in self.applied.values())
        if total > 1000:
            oldest = sorted((r.get("ts", now), u, g) for u, groups in self.applied.items() for g, r in groups.items())
            for _, u, g in oldest[: total - 1000]:
                self.applied.get(u, {}).pop(g, None)
        self.applied.setdefault(uid, {})[gid] = {"flag": flag, "ts": now, "name": name or gid}

    def drop_application(self, uid: str, gid: str):
        """清除指定用户的指定群申请记录（申请已落定）。"""
        groups = self.applied.get(uid)
        if groups:
            groups.pop(gid, None)
            if not groups:
                self.applied.pop(uid, None)

    def drop_application_by_flag(self, flag: str):
        """按 flag 清除申请记录（人工审批落定后调用）。"""
        if not flag:
            return
        for u in list(self.applied):
            groups = self.applied[u]
            for g in [g for g, r in groups.items() if r.get("flag") == flag]:
                groups.pop(g, None)
            if not groups:
                self.applied.pop(u, None)

    def find_earlier_application(self, uid: str, gid: str) -> str | None:
        """查找同一用户在其他群更早的待定申请，返回群名（无则返回 None）。"""
        recs = self.applied.get(uid, {})
        mine = recs.get(gid)
        if not mine:
            return None
        my_key = (mine.get("ts", 0), gid)
        earliest = None
        for g, r in recs.items():
            if g == gid:
                continue
            key = (r.get("ts", 0), g)
            if key < my_key and (earliest is None or key < earliest[0]):
                earliest = (key, r.get("name") or g)
        return earliest[1] if earliest else None
