#!/usr/bin/env python3
"""
交易指标查看工具
用法: python3 check_metrics.py [symbol]
"""
import sys
import json
from pathlib import Path
from utils.metrics import TradeMetrics

def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else "ETHUSDT"
    metrics_file = "metrics.jsonl"
    
    if not Path(metrics_file).exists():
        print(f"❌ 未找到交易文件: {metrics_file}")
        return
    
    metrics = TradeMetrics(symbol, metrics_file)
    stats = metrics.get_session_stats()
    
    print(f"\n{'='*60}")
    print(f"📊 {symbol} 交易统计")
    print(f"{'='*60}")
    print(f"总交易数:          {stats['total_trades']}")
    print(f"买单数/卖单数:      {stats['buy_count']} / {stats['sell_count']}")
    print(f"总买入数量/金额:    {stats['total_buy_qty']:.4f} / {stats['total_cost']:.2f}")
    print(f"总卖出数量/金额:    {stats['total_sell_qty']:.4f} / {stats['total_revenue']:.2f}")
    print(f"平均买价/卖价:      {stats['avg_buy_price']:.8f} / {stats['avg_sell_price']:.8f}")
    print(f"\n💰 已实现 P&L:      {stats['realized_pnl']:.2f} ({stats['pnl_pct']:.2f}%)")
    print(f"{'='*60}\n")

if __name__ == "__main__":
    main()

