from dataclasses import dataclass, field
from typing import List, Optional
from enum import Enum
import json
import os

from loguru import logger


class TradeState(Enum):
    HUNTING = "寻猎模式"              # State 0: 空仓等待入场
    ENTRY_SUBMITTING = "进场下单中"    # 过渡态: 首单已提交等确认
    GRID_ACTIVE = "网格防御中"         # State 1: 持仓 + 网格挂单活跃
    TTP_ARMED = "追踪猎杀"            # State 2: 追踪止盈已激活
    HIBERNATION = "深度休眠"           # State 3: 极限风控锁定


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
    symbol: str = ""
    state: TradeState = TradeState.HUNTING

    # 价格相关
    entry_price: float = 0.0       # 首单成交价
    avg_price: float = 0.0         # 整体持仓均价
    total_amount: float = 0.0      # 总持仓数量（币）

    # 入场快照 (策略 V2.0 新增)
    entry_timestamp: float = 0.0   # 入场时间戳 (用于72h时间衰减)
    snapshot_atr: float = 0.0      # 入场时刻的15m ATR快照 (用于网格定价)
    dynamic_base_volume: float = 0.0  # 本轮动态首单面值 (USDT)

    # 追踪止盈 (TTP) 核心变量
    highest_price: float = 0.0     # 激活止盈后的最高点

    # 网格相关
    active_grids: List[GridOrder] = field(default_factory=list)

    def reset(self):
        self.state = TradeState.HUNTING
        self.entry_price = 0.0
        self.avg_price = 0.0
        self.total_amount = 0.0
        self.entry_timestamp = 0.0
        self.snapshot_atr = 0.0
        self.dynamic_base_volume = 0.0
        self.highest_price = 0.0
        self.active_grids = []

    def save_to_disk(self, filename="state_backup.json"):
        """将当前内存状态存入硬盘"""
        data = {
            "symbol": self.symbol,
            "state": self.state.value,
            "entry_price": self.entry_price,
            "avg_price": self.avg_price,
            "total_amount": self.total_amount,
            "entry_timestamp": self.entry_timestamp,
            "snapshot_atr": self.snapshot_atr,
            "dynamic_base_volume": self.dynamic_base_volume,
            "highest_price": self.highest_price,
            "active_grids": [vars(g) for g in self.active_grids]
        }
        with open(filename, 'w') as f:
            json.dump(data, f)
        logger.debug("状态已备份到磁盘")

    def load_from_disk(self, filename="state_backup.json"):
        """从硬盘恢复状态"""
        if not os.path.exists(filename):
            return False
        try:
            with open(filename, 'r') as f:
                data = json.load(f)
                self.symbol = data.get('symbol', self.symbol)
                self.state = TradeState(data['state'])
                self.entry_price = data['entry_price']
                self.avg_price = data['avg_price']
                self.total_amount = data['total_amount']
                self.entry_timestamp = data.get('entry_timestamp', 0.0)
                self.snapshot_atr = data.get('snapshot_atr', 0.0)
                self.dynamic_base_volume = data.get('dynamic_base_volume', 0.0)
                self.highest_price = data['highest_price']
                self.active_grids = [GridOrder(**g) for g in data['active_grids']]
            logger.success("成功从本地文件恢复状态")
            return True
        except Exception as e:
            logger.error(f"恢复状态失败: {e}")
            return False