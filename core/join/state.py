import time

# 跨群申请记录有效期（秒）：QQ 进群申请过期后自动失效，不再阻拦其他群申请
APPLIED_TTL = 7 * 86400


class JoinState:
    """进群模块的运行时状态：失败计数、待审批登记、跨群申请记录。"""

    def __init__(self):
        # 进群尝试计数：{群_QQ: 次数}，带 24h 过期
        self.fail: dict[str, int] = {}
        self.fail_time: dict[str, float] = {}
        # 待人工审批的进群申请：{通知消息ID: {flag, gid, uid, nickname, ts}}
        self.pending: dict[str, dict] = {}
        # 跨群申请记录：{uid: {gid: {flag, ts, name}}}，用于多群同时申请判定
        self.applied: dict[str, dict[str, dict]] = {}

    def track_pending(self, key: str, gid: str, uid: str, nickname: str, flag: str):
        """登记一条待人工审批的进群申请，供管理员回复审批时精确匹配。"""
        self.pending[key] = {
            "gid": gid,
            "uid": uid,
            "nickname": nickname,
            "flag": flag,
            "ts": time.time(),
        }
        # 简单清理超过3天的过期记录，防止内存无限增长
        if len(self.pending) > 200:
            threshold = time.time() - 259200
            stale = [k for k, v in self.pending.items() if v["ts"] < threshold]
            for k in stale:
                self.pending.pop(k, None)

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
