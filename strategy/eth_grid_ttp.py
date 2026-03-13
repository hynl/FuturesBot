import asyncio
import time

import pandas as pd
import pandas_ta_remake as ta
from loguru import logger
from core.position_manager import SessionState, TradeState, GridOrder


class EthGridStrategy:
    def __init__(self, exchange_wrapper, config: dict, metrics_callback=None,
                 hibernation_callback=None, telegram=None):
        self.ex = exchange_wrapper
        self.config = config
        self.state = SessionState()
        self.state.symbol = config.get('symbol', self.state.symbol)
        self.lock = asyncio.Lock()
        self.metrics_callback = metrics_callback
        self.hibernation_callback = hibernation_callback  # main.py 注入的冻结回调
        self.tg = telegram  # TelegramBot 实例 (可选)
        self._is_frozen = False  # HIBERNATION 后冻结一切策略计算

        # ── 指标参数 ──
        self.rsi_period = config.get('rsi_period', 14)
        self.rsi_oversold = config.get('rsi_oversold', 40.0)
        self.rsi_overbought = config.get('rsi_overbought', 70.0)
        self.atr_period = config.get('atr_period', 14)

        # ── 趋势过滤 (V2.0) ──
        self.trend_filter_enabled = config.get('trend_filter_enabled', True)
        self.trend_ema_period = config.get('trend_ema_period', 200)

        # ── 动态仓位计算器 (V2.0) ──
        self.base_notional = config.get('base_notional', 300.0)
        self.baseline_atr = config.get('baseline_atr', 15.0)
        self.dynamic_sizer_min = config.get('dynamic_sizer_min', 150.0)
        self.dynamic_sizer_max = config.get('dynamic_sizer_max', 450.0)

        # ── 网格参数 ──
        self.safety_notional = config.get('safety_notional', 450.0)
        self.volume_multiplier = config.get('volume_multiplier', 1.5)
        self.max_safety_trades = config.get('max_safety_trades', 4)
        self.grid_ratios = config.get('grid_ratios', [1.0, 1.8, 3.0, 5.0])[:self.max_safety_trades]
        fallback = [round(self.volume_multiplier ** (i + 1), 6) for i in range(self.max_safety_trades)]
        self.grid_multipliers = config.get('grid_multipliers', fallback)[:self.max_safety_trades]

        # ── TTP 止盈 (V2.0 含时间衰减) ──
        self.ttp_activation_profit_pct = config.get('ttp_activation_profit_pct', 1.5)
        self.ttp_trailing_loss_pct = config.get('ttp_trailing_loss_pct', 0.3)
        self.ttp_time_decay_hours = config.get('ttp_time_decay_hours', 72.0)
        self.ttp_time_decay_profit_pct = config.get('ttp_time_decay_profit_pct', 0.2)

        # ── T4 击穿缓冲 (V2.0) ──
        self.t4_buffer_pct = config.get('t4_buffer_pct', 0.5)

        # ── 4H 趋势数据缓存 (由外部注入) ──
        self._ema200_4h: float | None = None

        logger.info(
            f"策略V2.0初始化: RSI({self.rsi_period}) <{self.rsi_oversold} | ATR({self.atr_period}) baseline={self.baseline_atr} | "
            f"趋势过滤={'ON' if self.trend_filter_enabled else 'OFF'} EMA{self.trend_ema_period} | "
            f"网格{len(self.grid_ratios)}层 | TTP {self.ttp_activation_profit_pct}%/{self.ttp_trailing_loss_pct}% 72h衰减→{self.ttp_time_decay_profit_pct}% | "
            f"T4缓冲{self.t4_buffer_pct}%"
        )

    # ================================================================
    #  外部注入接口
    # ================================================================

    def update_trend_ema(self, ema_value: float | None):
        """由 main.py 注入最新的 4H EMA200 值"""
        self._ema200_4h = ema_value

    # ================================================================
    #  纯函数模块 — 趋势过滤器 & 动态仓位
    # ================================================================

    def check_trend_filter(self, curr_price: float) -> bool:
        """宏观趋势过滤: price > 4H EMA200"""
        if not self.trend_filter_enabled:
            return True
        if self._ema200_4h is None:
            logger.warning("4H EMA200 尚未就绪，趋势过滤跳过")
            return False
        passed = curr_price > self._ema200_4h
        if not passed:
            logger.debug(f"趋势过滤未通过: price={curr_price:.2f} <= EMA200={self._ema200_4h:.2f}")
        return passed

    def calc_dynamic_base_volume(self, realtime_atr: float) -> float:
        """动态首单计算器: base * (baseline / realtime_atr), clamp [min, max]"""
        if realtime_atr <= 0:
            return self.base_notional
        raw = self.base_notional * (self.baseline_atr / realtime_atr)
        clamped = max(self.dynamic_sizer_min, min(self.dynamic_sizer_max, raw))
        logger.info(f"动态仓位: ATR={realtime_atr:.2f} → raw={raw:.1f} → clamped={clamped:.1f} USDT")
        return clamped

    def _get_effective_ttp_activation(self) -> float:
        """根据持仓时长决定 TTP 激活线 (72h 时间衰减)"""
        if self.state.entry_timestamp <= 0:
            return self.ttp_activation_profit_pct
        hours_held = (time.time() - self.state.entry_timestamp) / 3600.0
        if hours_held > self.ttp_time_decay_hours:
            logger.info(f"⏰ 持仓 {hours_held:.1f}h > {self.ttp_time_decay_hours}h, TTP激活线降至 {self.ttp_time_decay_profit_pct}%")
            return self.ttp_time_decay_profit_pct
        return self.ttp_activation_profit_pct

    # ================================================================
    #  K线闭合回调 (State 0: HUNTING 入口)
    # ================================================================

    async def on_kline_closed(self, df: pd.DataFrame):
        """每当 15m K线闭合时调用"""
        if self._is_frozen:
            return
        df.ta.rsi(length=self.rsi_period, append=True)
        df.ta.atr(length=self.atr_period, append=True)

        last_row = df.iloc[-1]
        curr_rsi = last_row[f'RSI_{self.rsi_period}']
        curr_atr = last_row[f'ATRr_{self.atr_period}']
        curr_price = last_row['close']

        logger.info(f"K线闭合: price={curr_price}, RSI={curr_rsi:.2f}, ATR={curr_atr:.2f}, state={self.state.state.name}")

        # State 0: HUNTING — 寻猎入场
        if self.state.state == TradeState.HUNTING:
            # AND 逻辑: 趋势 + RSI
            if not self.check_trend_filter(curr_price):
                return
            if curr_rsi < self.rsi_oversold:
                await self.execute_entry(curr_price, curr_atr)

        # State 1: GRID_ACTIVE — 也在 K线闭合时检查一次
        elif self.state.state == TradeState.GRID_ACTIVE:
            await self.check_grid_and_ttp(curr_price)

    # ================================================================
    #  State 0 → State 1: 入场执行
    # ================================================================

    async def execute_entry(self, price: float, atr: float):
        """State 0 HUNTING → State 1 GRID_ACTIVE"""
        symbol = self.state.symbol
        # 1. 动态首单
        dynamic_vol = self.calc_dynamic_base_volume(atr)
        base_qty = dynamic_vol / price

        logger.info(f"🚀 入场: price={price:.2f}, 动态面值={dynamic_vol:.1f}U, qty={base_qty:.4f}")

        # 2. 实际市价买入
        try:
            order = await self.ex.create_market_order(symbol, 'buy', base_qty)
            fill_price = float(order.get('average', price))
            fill_qty = float(order.get('filled', base_qty))
        except Exception as e:
            logger.error(f"首单市价买入失败: {e}")
            return  # 下单失败不进入 GRID_ACTIVE

        # 3. 快照
        self.state.entry_price = fill_price
        self.state.avg_price = fill_price
        self.state.total_amount = fill_qty
        self.state.entry_timestamp = time.time()
        self.state.snapshot_atr = atr
        self.state.dynamic_base_volume = dynamic_vol
        self.state.state = TradeState.ENTRY_SUBMITTING

        # 4. 网格限价挂单 (使用 Snapshot_ATR)
        for i, ratio in enumerate(self.grid_ratios):
            grid_price = fill_price - (ratio * atr)
            grid_qty = fill_qty * self.grid_multipliers[i]

            try:
                grid_order = await self.ex.create_limit_order(symbol, 'buy', grid_qty, grid_price)
                order_id = grid_order.get('id', '')
            except Exception as e:
                logger.error(f"T{i+1} 限价挂单失败: {e}")
                order_id = ''

            grid_item = GridOrder(level=i + 1, price=grid_price, amount=grid_qty, order_id=order_id)
            self.state.active_grids.append(grid_item)
            logger.info(f"📍 T{i+1}: price={grid_price:.2f}, qty={grid_qty:.4f}, id={order_id}")

        self.state.state = TradeState.GRID_ACTIVE
        self.state.save_to_disk()
        logger.success(f"✅ GRID_ACTIVE, snapshot_atr={atr:.2f}, grids={len(self.state.active_grids)}")

        # Telegram 告警
        if self.tg:
            self.tg.alert_entry(symbol, fill_price, fill_qty, dynamic_vol, atr)
            self.tg.alert_grid_placed(symbol, self.state.active_grids)

    # ================================================================
    #  State 1: GRID_ACTIVE 核心循环
    # ================================================================

    async def check_grid_and_ttp(self, curr_price: float):
        """State 1 循环: 检查 T4击穿 / TTP激活 / TTP追踪"""
        if self._is_frozen:
            return

        # ── 条件 C: T4 击穿 (带 0.5% 缓冲) ──
        if self.state.active_grids:
            t4_price = self.state.active_grids[-1].price
            t4_breach_line = t4_price * (1 - self.t4_buffer_pct / 100.0)
            if curr_price < t4_breach_line:
                logger.critical(f"🚨 T4击穿! price={curr_price:.2f} < T4={t4_price:.2f} * (1-{self.t4_buffer_pct}%) = {t4_breach_line:.2f}")
                await self.trigger_hibernation()
                return

        # ── 计算浮盈 ──
        profit_pct = (curr_price - self.state.avg_price) / self.state.avg_price * 100

        # ── 根据持仓时长选择激活线 ──
        effective_activation = self._get_effective_ttp_activation()

        # ── 条件 A/B: TTP 激活 ──
        if self.state.state == TradeState.GRID_ACTIVE:
            if profit_pct >= effective_activation:
                self.state.state = TradeState.TTP_ARMED
                self.state.highest_price = curr_price
                hours_held = (time.time() - self.state.entry_timestamp) / 3600.0
                logger.warning(
                    f"🔥 TTP_ARMED! profit={profit_pct:.2f}% >= {effective_activation}%, "
                    f"held={hours_held:.1f}h, avg={self.state.avg_price:.2f}"
                )
                # Telegram 告警
                if self.tg:
                    self.tg.alert_ttp_armed(self.state.symbol, profit_pct, self.state.avg_price, hours_held)

        # ── State 2: TTP_ARMED 追踪 ──
        elif self.state.state == TradeState.TTP_ARMED:
            if curr_price > self.state.highest_price:
                self.state.highest_price = curr_price

            trailing_stop = self.state.highest_price * (1 - self.ttp_trailing_loss_pct / 100.0)
            if curr_price <= trailing_stop:
                logger.success(
                    f"💰 猎杀触发! price={curr_price:.2f} <= stop={trailing_stop:.2f} "
                    f"(high={self.state.highest_price:.2f} * {1 - self.ttp_trailing_loss_pct/100:.4f})"
                )
                await self.execute_take_profit(curr_price)

    # ================================================================
    #  State 3: HIBERNATION
    # ================================================================

    async def trigger_hibernation(self):
        """
        State 1 → State 3: HIBERNATION (深度休眠)
        策略文档要求: 按严格顺序执行且不可逆
        """
        symbol = self.state.symbol
        reserve = self.config.get('reserve_capital_usdt', 1700.0)

        # ── Step 1: 冻结策略计算 ──
        self._is_frozen = True
        self.state.state = TradeState.HIBERNATION
        self.state.save_to_disk()
        logger.critical("🚨 [HIBERNATION Step 1/5] 策略计算已冻结，停止一切 K 线解析!")

        # ── Step 2: 划转备用金 Spot → Futures ──
        try:
            transfer_ok = await self.ex.client.transfer(
                code='USDT',
                amount=reserve,
                fromAccount='spot',
                toAccount='future',
            )
            logger.critical(f"🚨 [HIBERNATION Step 2/5] 划转 {reserve} USDT: Spot→Futures 成功")
        except Exception as e:
            logger.critical(f"🚨 [HIBERNATION Step 2/5] 划转失败: {e} (仍继续后续步骤)")

        # ── Step 3: 撤销所有挂单 ──
        try:
            await self.ex.cancel_all_orders(symbol)
            logger.critical(f"🚨 [HIBERNATION Step 3/5] 已撤销 {symbol} 所有挂单")
        except Exception as e:
            logger.critical(f"🚨 [HIBERNATION Step 3/5] 撤单失败: {e}")

        # ── Step 4: CRITICAL 报警 (含 Telegram) ──
        logger.critical(
            f"🚨🚨🚨 [HIBERNATION Step 4/5] CRITICAL ALERT: "
            f"T4防线击穿! symbol={symbol}, avg={self.state.avg_price:.2f}, "
            f"total={self.state.total_amount:.4f} — 需要人工介入!"
        )
        if self.tg:
            await self.tg.alert_hibernation(symbol, self.state.avg_price, self.state.total_amount)

        # ── Step 5: 通知 main.py 进入死循环 ──
        logger.critical("🚨 [HIBERNATION Step 5/5] 通知主程序进入休眠死循环, 等待人工重启...")
        if self.hibernation_callback:
            await self.hibernation_callback()

    # ================================================================
    #  State 2 → State 0: 止盈平仓
    # ================================================================

    async def execute_take_profit(self, price: float):
        """TTP_ARMED → HUNTING: 市价平仓 + 撤单"""
        symbol = self.state.symbol
        logger.success(f"💰 TTP止盈平仓! price={price:.2f}, avg={self.state.avg_price:.2f}, qty={self.state.total_amount:.4f}")

        # Telegram 告警
        if self.tg:
            self.tg.alert_take_profit(self.state.symbol, price, self.state.avg_price, self.state.total_amount)

        # 1. 市价卖出平仓
        try:
            await self.ex.create_market_order(symbol, 'sell', self.state.total_amount)
        except Exception as e:
            logger.error(f"止盈市价卖出失败: {e}")
            return  # 不清状态，下次循环重试

        # 2. 撤销所有剩余挂单 (策略文档: 必选动作)
        try:
            await self.ex.cancel_all_orders(symbol)
        except Exception as e:
            logger.error(f"撤销挂单失败: {e}")

        self.state.reset()
        self.state.save_to_disk()

    # ================================================================
    #  订单成交回调
    # ================================================================

    async def on_order_update(self, order_data):
        """User Stream 订单推送回调"""
        async with self.lock:
            status = order_data['X']
            if status != 'FILLED':
                return

            side = order_data['S']
            price = float(order_data['L'])
            qty = float(order_data['l'])
            timestamp = order_data.get('T', 0) / 1000.0

            logger.success(f"✅ 成交: {side} {qty} @ {price}")
            if self.metrics_callback:
                self.metrics_callback(side=side, price=price, qty=qty, timestamp=timestamp)

            # 策略文档要求: 必须通过 API 同步 avg_price, 不能用本地公式
            try:
                api_avg = await self.ex.get_api_entry_price(self.state.symbol)
                if api_avg > 0:
                    self.state.avg_price = api_avg
            except Exception as e:
                logger.warning(f"API 均价同步失败, 降级本地计算: {e}")
                # 降级: 仅在 API 失败时用本地公式
                if side == 'BUY':
                    new_total = self.state.total_amount + qty
                    self.state.avg_price = (
                        (self.state.avg_price * self.state.total_amount) + (price * qty)
                    ) / new_total

            # 更新总持仓
            if side == 'BUY':
                self.state.total_amount += qty
            elif side == 'SELL':
                self.state.total_amount = max(0.0, self.state.total_amount - qty)

            # 标记对应网格单为已成交
            for g in self.state.active_grids:
                if not g.filled and abs(g.price - price) / g.price < 0.005:
                    g.filled = True
                    logger.info(f"📍 T{g.level} 已成交")
                    break

            self.state.save_to_disk()
            logger.info(f"🔄 avg={self.state.avg_price:.2f} (API), total={self.state.total_amount:.4f}")

    # ================================================================
    #  热更新
    # ================================================================

    async def update_parameters(self, new_config: dict):
        """热更新策略参数"""
        rsi_cfg = new_config.get("rsi", {})
        atr_cfg = new_config.get("atr", {})
        grid_cfg = new_config.get("grid", {})
        ttp_cfg = new_config.get("ttp", {})
        safety_cfg = new_config.get("safety", {})
        sizer_cfg = new_config.get("dynamic_sizer", {})
        t4_cfg = new_config.get("t4_breach", {})

        self.rsi_period = int(rsi_cfg.get("period", self.rsi_period))
        self.rsi_oversold = float(rsi_cfg.get("oversold", self.rsi_oversold))
        self.atr_period = int(atr_cfg.get("period", self.atr_period))

        self.baseline_atr = float(sizer_cfg.get("baseline_atr", self.baseline_atr))
        self.dynamic_sizer_min = float(sizer_cfg.get("min_notional", self.dynamic_sizer_min))
        self.dynamic_sizer_max = float(sizer_cfg.get("max_notional", self.dynamic_sizer_max))

        self.volume_multiplier = float(safety_cfg.get("volume_multiplier", self.volume_multiplier))
        self.max_safety_trades = int(safety_cfg.get("max_trades", self.max_safety_trades))
        self.grid_ratios = grid_cfg.get("ratios", self.grid_ratios)[:self.max_safety_trades]
        fb = [round(self.volume_multiplier ** (i + 1), 6) for i in range(self.max_safety_trades)]
        self.grid_multipliers = grid_cfg.get("multipliers", fb)[:self.max_safety_trades]

        self.ttp_activation_profit_pct = float(ttp_cfg.get("activation_profit_pct", self.ttp_activation_profit_pct))
        self.ttp_trailing_loss_pct = float(ttp_cfg.get("trailing_loss_pct", self.ttp_trailing_loss_pct))
        self.ttp_time_decay_hours = float(ttp_cfg.get("time_decay_hours", self.ttp_time_decay_hours))
        self.ttp_time_decay_profit_pct = float(ttp_cfg.get("time_decay_profit_pct", self.ttp_time_decay_profit_pct))
        self.t4_buffer_pct = float(t4_cfg.get("buffer_pct", self.t4_buffer_pct))

        logger.warning(f"🔄 策略V2.0参数已热更新")
