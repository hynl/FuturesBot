#!/usr/bin/env python3
"""
热配置更新演示脚本
展示如何在 bot 运行中修改参数
"""
import time
import sys

def main():
    print("""
╔════════════════════════════════════════════════════════════════╗
║         🔄 FuturesBot 热配置更新使用指南                       ║
╚════════════════════════════════════════════════════════════════╝

📋 工作原理
─────────────────────────────────────────────────────────────────
1. bot 启动时：ConfigWatcher 监听 config/settings.yaml
2. 你修改文件：编辑 settings.yaml 中的参数
3. 自动热更新：5秒内自动检测到变更并应用
4. 无需重启：整个过程对交易零中断

🚀 快速演示步骤
─────────────────────────────────────────────────────────────────

1️⃣  启动 bot
   $ python3 main.py
   
   注意观察日志输出，应该看到：
   ✅ ConfigWatcher 初始化: config/settings.yaml
   ✅ ConfigWatcher 已启动，每 5 秒检查一次配置变更...

2️⃣  在另一个终端修改参数
   $ vi config/settings.yaml
   
   修改任意参数，比如：
   rsi:
     oversold: 25          # 从 30 改成 25
   
   ttp:
     activation_profit_pct: 1.0  # 从 1.5 改成 1.0

3️⃣  观察 bot 的日志
   你应该看到：
   🔄 策略参数已热更新: RSI(14->14) 超卖(30->25) TTP(1.5%->(1.0%)%)

4️⃣  验证参数生效
   无需重启，新参数立即生效！

📊 支持热更新的参数
─────────────────────────────────────────────────────────────────
✅ RSI 配置
   - period: 周期（默认 14）
   - oversold: 超卖线（默认 30）
   - overbought: 超买线（默认 70）

✅ ATR 配置
   - period: 周期（默认 14）

✅ 网格配置
   - ratios: 跌幅系数
   - multipliers: 层级倍数

✅ TTP 止盈配置
   - activation_profit_pct: 激活线（默认 1.5%）
   - trailing_loss_pct: 回撤线（默认 0.3%）

❌ 不支持热更新（需要重启）
   - trade: symbol、base_asset（涉及交易所连接重新初始化）
   - runtime: testnet、kline_interval（涉及 WS 重新订阅）
   - 其他影响启动的全局参数

⚠️  注意事项
─────────────────────────────────────────────────────────────────
1. 修改 settings.yaml 时保持 YAML 格式正确
   - 不要破坏缩进
   - 不要添加中文注释（可能编码问题）

2. 不要在 bot 运行中修改 trade/runtime 参数
   - 这些参数涉及深层初始化
   - 需要重启 bot 才能生效

3. 监听间隔是 5 秒
   - 修改后最多 5 秒内生效
   - 可在 utils/config_watcher.py 中调整 check_interval

🔧 高级用法
─────────────────────────────────────────────────────────────────
可以为不同的市场行情预设多套参数配置：

【震荡行情】
rsi:
  oversold: 35
ttp:
  activation_profit_pct: 2.0
  trailing_loss_pct: 0.5

【趋势行情】
rsi:
  oversold: 25
ttp:
  activation_profit_pct: 1.0
  trailing_loss_pct: 0.2

根据市场情况快速切换即可！

─────────────────────────────────────────────────────────────────
需要帮助？查看 PROGRESS.md 了解更多信息
    """)

if __name__ == "__main__":
    main()

