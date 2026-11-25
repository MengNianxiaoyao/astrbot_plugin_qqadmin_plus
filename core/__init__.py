"""业务能力层：每个模块只处理一类群管事务，不依赖 AstrBot 的命令注册。

- normal_handle：禁言/头衔/踢人/拉黑/管理员/精华/头像/群名
- recall / notice：撤回与群公告
- member_handle / file_handle：群友信息与群文件
- banpro：违禁词、刷屏、投票（子模块分治）
- join / curfew / lottery：有状态的长流程，各自内部再分层

本层接受 event 和配置/存储依赖，返回待发送文本或直接调用 OneBot；
权限、事件终止与命令注册统一由 ``main.py`` 的 AstrBot 接口层负责。
"""

from .banpro import BanproHandle
from .curfew import CurfewHandle
from .file_handle import FileHandle
from .join import JoinHandle
from .lottery import LotteryHandle
from .member_handle import MemberHandle
from .normal_handle import NormalHandle
from .notice_handle import NoticeHandle
from .recall_handle import RecallHandle

__all__ = [
    "BanproHandle",
    "CurfewHandle",
    "FileHandle",
    "JoinHandle",
    "LotteryHandle",
    "MemberHandle",
    "NormalHandle",
    "NoticeHandle",
    "RecallHandle",
]
