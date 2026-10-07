"""群配置存储：SQLite 按群存 JSON + 全局名单 JSON。

跟随默认设计（新人必读）：
- 无行 = 跟随默认：get_group_snapshot 返回 default_cfg 深拷贝。
- 有行必带 __follow_default__=False = 显式独立配置。
- 不按值比较（显式值恰好等于默认值也不算跟随）；历史无 marker 行
  只在 init 一次性迁移时按值判定，之后不再使用。
- 读：all/get/get_group_snapshot（快照自动补齐新增字段并写回）；
  写：set/replace_group/add；删：delete_group/follow_default/reset_to_default。

子模块：group_db.QQAdminDB / global_list.QQAdminGlobalList。
"""

from .global_list import QQAdminGlobalList
from .group_db import QQAdminDB

__all__ = ["QQAdminDB", "QQAdminGlobalList"]
