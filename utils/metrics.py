import json
import os
from datetime import datetime
from typing import Dict, List, Optional
from loguru import logger


class TradeRecord:
    """单条交易记录"""
    def __init__(self, symbol: str, side: str, price: float, qty: float, timestamp: float):
        self.symbol = symbol
        self.side = side
        self.price = price
        self.qty = qty
        self.timestamp = timestamp

    def to_dict(self):
        return {
            "symbol": self.symbol,
            "side": self.side,
            "price": self.price,
            "qty": self.qty,
            "timestamp": self.timestamp,
            "time": datetime.fromtimestamp(self.timestamp).isoformat(),
        }


class TradeMetrics:
    """交易性能指标收集器"""
    
    def __init__(self, symbol: str, metrics_file: str = "metrics.jsonl"):
        self.symbol = symbol
        self.metrics_file = metrics_file
        self.trades: List[TradeRecord] = []
        
        # 初始化时加载已有记录
        self._load_trades()
    
    def _load_trades(self):
        """从文件加载历史交易记录"""
        if not os.path.exists(self.metrics_file):
            return
        try:
            with open(self.metrics_file, 'r') as f:
                for line in f:
                    if line.strip():
                        data = json.loads(line)
                        if data.get('symbol') == self.symbol:
                            trade = TradeRecord(
                                symbol=data['symbol'],
                                side=data['side'],
                                price=data['price'],
                                qty=data['qty'],
                                timestamp=data['timestamp']
                            )
                            self.trades.append(trade)
            logger.info(f"✅ 加载 {self.symbol} 的 {len(self.trades)} 条历史交易记录")
        except Exception as e:
            logger.error(f"❌ 加载交易记录失败: {e}")
    
    def record_trade(self, side: str, price: float, qty: float, timestamp: float = None):
        """记录一笔交易"""
        if timestamp is None:
            timestamp = datetime.now().timestamp()
        
        trade = TradeRecord(self.symbol, side, price, qty, timestamp)
        self.trades.append(trade)
        
        # 异步写入文件
        self._append_to_file(trade)
    
    def _append_to_file(self, trade: TradeRecord):
        """追加交易记录到 JSONL 文件"""
        try:
            with open(self.metrics_file, 'a') as f:
                f.write(json.dumps(trade.to_dict()) + '\n')
        except Exception as e:
            logger.error(f"❌ 写入交易记录失败: {e}")
    
    def get_session_stats(self) -> Dict:
        """获取当前会话统计"""
        if len(self.trades) == 0:
            return {
                "symbol": self.symbol,
                "total_trades": 0,
                "buy_count": 0,
                "sell_count": 0,
                "total_buy_qty": 0.0,
                "total_sell_qty": 0.0,
                "avg_buy_price": 0.0,
                "avg_sell_price": 0.0,
            }
        
        buy_trades = [t for t in self.trades if t.side.upper() == 'BUY']
        sell_trades = [t for t in self.trades if t.side.upper() == 'SELL']
        
        total_buy_qty = sum(t.qty for t in buy_trades)
        total_sell_qty = sum(t.qty for t in sell_trades)
        total_buy_cost = sum(t.price * t.qty for t in buy_trades)
        total_sell_revenue = sum(t.price * t.qty for t in sell_trades)
        
        avg_buy_price = total_buy_cost / total_buy_qty if total_buy_qty > 0 else 0.0
        avg_sell_price = total_sell_revenue / total_sell_qty if total_sell_qty > 0 else 0.0
        
        pnl = total_sell_revenue - total_buy_cost
        pnl_pct = (pnl / total_buy_cost * 100) if total_buy_cost > 0 else 0.0
        
        return {
            "symbol": self.symbol,
            "total_trades": len(self.trades),
            "buy_count": len(buy_trades),
            "sell_count": len(sell_trades),
            "total_buy_qty": total_buy_qty,
            "total_sell_qty": total_sell_qty,
            "avg_buy_price": round(avg_buy_price, 8),
            "avg_sell_price": round(avg_sell_price, 8),
            "total_cost": round(total_buy_cost, 2),
            "total_revenue": round(total_sell_revenue, 2),
            "realized_pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
        }
    
    def print_stats(self):
        """打印统计信息"""
        stats = self.get_session_stats()
        logger.info(
            "📊 {symbol} 交易统计: 总交易={total_trades}, "
            "买={buy_count} 卖={sell_count}, "
            "均价 买={avg_buy_price} 卖={avg_sell_price}, "
            "已实现P&L={realized_pnl} ({pnl_pct}%)".format(**stats)
        )

