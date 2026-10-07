"""QQ 群信息缓存：多 aiocqhttp 连接聚合，带 TTL 与 DB 兜底。

刷新管线（cache.QQGroupInfoCache._refresh_group_list）：
1) 多 client 并行 get_group_list 合并去重。
2) 实时拿不到的已知群用 DB 兜底（source=cached）。
3) 缺详情（无名/人数全 0）限流补 get_group_info。
4) Bot 身份（bot_role）单独懒补，TTL 600s。
纯数据变换在 .utils（可单测）；失效判定连续计票防抖动误删。

子模块：cache.QQGroupInfoCache / utils（归一化/排序/错误格式化）。
"""

from .cache import QQGroupInfoCache

__all__ = ["QQGroupInfoCache"]
