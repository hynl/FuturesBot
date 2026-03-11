from dataclasses import dataclass, field
from typing import List, Optional
from enum import Enum


class TradeState(Enum):
    IDLE = "空仓"
    ENTRY_SUBMITTING = "进场下单中"
    GRID_WAITING = "网格补仓中"
    TTP_ACTIVATED = "追踪止盈已激活"
    BLACKOUT = "极限风控锁定"  # 第4层破位后的“装死”状态


@dataclass
class GridOrder:
    level: int  # 1, 2, 3, 4 层
    price: float  # 挂单价格
    amount: float  # 币数
    order_id: str = ""  # 交易所订单ID
    filled: bool = False


@dataclass
class SessionState:
    """当前交易会话的实时状态内存镜像"""
    symbol: str = "ETHUSDT"
    state: TradeState = TradeState.IDLE

    # 价格相关
    entry_price: float = 0.0  # 首单成交价
    avg_price: float = 0.0  # 整体持仓均价
    total_amount: float = 0.0  # 总持仓数量

    # 追踪止盈 (TTP) 核心变量
    highest_price: float = 0.0  # 激活止盈后的最高点

    # 网格相关
    active_grids: List[GridOrder] = field(default_factory=list)

    def reset(self):
        self.state = TradeState.IDLE
        self.entry_price = 0.0
        self.avg_price = 0.0
        self.total_amount = 0.0
        self.highest_price = 0.0
        self.active_grids = []