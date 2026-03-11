import pandas as pd
import pandas_ta as ta
from loguru import logger
from core.position_manager import SessionState, TradeState, GridOrder


class EthGridStrategy:
    def __init__(self, exchange_wrapper, config: dict):
        self.ex = exchange_wrapper  # 之前封装的 BinanceExchange
        self.config = config
        self.state = SessionState()

    async def on_kline_closed(self, df: pd.DataFrame):
        """
        每当 15m K线闭合时调用。
        df: 包含 ['open', 'high', 'low', 'close', 'volume'] 的 DataFrame
        """
        # 1. 计算指标
        df.ta.rsi(length=14, append=True)
        df.ta.atr(length=14, append=True)

        last_row = df.iloc[-1]
        curr_rsi = last_row['RSI_14']
        curr_atr = last_row['ATRr_14']
        curr_price = last_row['close']

        logger.info(f"K线闭合: 价格={curr_price}, RSI={curr_rsi:.2f}, ATR={curr_atr:.2f}")

        # 2. 状态机逻辑
        if self.state.state == TradeState.IDLE:
            if curr_rsi < 40:
                await self.execute_entry(curr_price, curr_atr)

        elif self.state.state == TradeState.GRID_WAITING:
            await self.check_grid_and_ttp(curr_price)

    async def execute_entry(self, price: float, atr: float):
        """执行首单入场并计算 4 层非对称网格"""
        logger.info(f"🚀 RSI < 40 触发入场逻辑，当前价格: {price}")

        # 设定面值 300U (注意：实际下单需转换为币数 = 300 / price)
        base_qty = self.config['base_notional'] / price

        # 1. 市价开仓 (这里简化，实际需要调用 self.ex.client.create_order)
        # order = await self.ex.client.create_market_buy_order(...)

        self.state.entry_price = price
        self.state.avg_price = price
        self.state.total_amount = base_qty
        self.state.state = TradeState.ENTRY_SUBMITTING

        # 2. 计算 4 层非对称网格价格与金额 (1.5倍递增)
        # 跌幅系数: 1.0, 1.8, 3.0, 5.0
        ratios = [1.0, 1.8, 3.0, 5.0]
        multipliers = [1.5, 2.25, 3.375, 5.06]  # 1.5^n

        for i, ratio in enumerate(ratios):
            grid_price = price - (ratio * atr)
            grid_qty = base_qty * multipliers[i]

            # 实际生产环境下需在此挂 Limit Buy 单
            grid_item = GridOrder(level=i + 1, price=grid_price, amount=grid_qty)
            self.state.active_grids.append(grid_item)
            logger.info(f"📍 预设第 {i + 1} 层补仓单: 价格 {grid_price:.2f}, 数量 {grid_qty:.3f}")

        self.state.state = TradeState.GRID_WAITING

    async def check_grid_and_ttp(self, curr_price: float):
        """
        在网格周期内，每秒/每Tick 监听价格。
        1. 检查是否有网格单成交 (更新均价)。
        2. 检查是否达到 TTP 激活线。
        3. 检查是否跌破第 4 层。
        """
        # 检查风控：如果跌破第 4 层
        if curr_price < self.state.active_grids[-1].price:
            await self.trigger_blackout()
            return

        # 计算当前浮盈
        profit_pct = (curr_price - self.state.avg_price) / self.state.avg_price * 100

        # 追踪止盈逻辑
        if self.state.state != TradeState.TTP_ACTIVATED:
            if profit_pct >= 1.5:
                self.state.state = TradeState.TTP_ACTIVATED
                self.state.highest_price = curr_price
                logger.warning(f"🔥 TTP 激活！当前获利 {profit_pct:.2f}%, 均价 {self.state.avg_price}")
        else:
            # 已进入 TTP 状态，更新最高点
            if curr_price > self.state.highest_price:
                self.state.highest_price = curr_price
                logger.info(f"📈 更新 TTP 最高价: {curr_price}")

            # 检查回撤 0.3%
            drop_from_high = (self.state.highest_price - curr_price) / self.state.highest_price * 100
            if drop_from_high >= 0.3:
                await self.execute_take_profit(curr_price)

    async def trigger_blackout(self):
        """极限风控：进入休眠并报警"""
        self.state.state = TradeState.BLACKOUT
        logger.critical("🚨 价格跌破第 4 层防线！触发休眠风控，停止所有交易！")
        # 此处预留调用 self.ex.transfer_from_spot() 划转 1700U 接口

    async def execute_take_profit(self, price: float):
        """清仓结账"""
        logger.success(f"💰 TTP 触发止盈平仓！平仓价: {price}")
        # await self.ex.client.create_market_sell_order(...)
        # 撤销所有未成交的网格挂单
        # await self.ex.cancel_all_orders()
        self.state.reset()