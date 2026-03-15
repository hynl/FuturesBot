import unittest
from unittest.mock import MagicMock, AsyncMock, patch
import asyncio
import time
import sys
import os

# Ensure parent directory is in path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from strategy.eth_grid_ttp import EthGridStrategy
from core.position_manager import TradeState, GridOrder

class TestEthGridStrategyLogic(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Mock Exchange
        self.mock_ex = MagicMock()
        self.mock_ex.client = MagicMock()
        self.mock_ex.client.fetch_ohlcv = AsyncMock(return_value=[])
        
        # Mock Exchange helper methods
        self.mock_ex.price_to_precision = MagicMock(side_effect=lambda s, p: f"{p:.2f}")
        self.mock_ex.amount_to_precision = MagicMock(side_effect=lambda s, a: f"{a:.4f}")
        self.mock_ex.create_market_order = AsyncMock(return_value={'average': 2100.0, 'filled': 0.1})
        self.mock_ex.create_limit_order = AsyncMock(return_value={'id': 'mock_id'})
        self.mock_ex.client.transfer = AsyncMock(return_value={'id': 'transfer_id'})

        # Default Config
        self.config = {
            'symbol': 'ETH/USDT',
            'base_notional': 300.0,
            'baseline_atr': 15.0,
            'dynamic_sizer_min': 150.0,
            'dynamic_sizer_max': 450.0,
            'ttp_activation_profit_pct': 1.5,
            'ttp_time_decay_hours': 72.0,
            'ttp_time_decay_profit_pct': 0.2,
            # Grid settings
            'max_safety_trades': 5,
            'grid_multipliers': [1.0, 1.0, 1.0, 1.0, 1.0] 
        }

        self.strategy = EthGridStrategy(self.mock_ex, self.config)

    def test_dynamic_notional_sizing(self):
        print("\n=== 测试场景 1: 动态面值计算 (Running) ===")
        
        # Case 1: ATR = Baseline (15.0) -> Should be Base Notional (300)
        vol_baseline = self.strategy.calc_dynamic_base_volume(15.0)
        self.assertAlmostEqual(vol_baseline, 300.0)
        
        # Case 2: ATR Low (5.0) -> Should be Higher (900 -> Max Clamped 450)
        vol_low_atr = self.strategy.calc_dynamic_base_volume(5.0)
        self.assertEqual(vol_low_atr, 450.0)

        # Case 3: ATR High (50.0) -> Should be Lower (90 -> Min Clamped 150)
        vol_high_atr = self.strategy.calc_dynamic_base_volume(50.0)
        self.assertEqual(vol_high_atr, 150.0)

        # Case 4: ATR Zero/Negative -> Fallback to Base
        vol_zero = self.strategy.calc_dynamic_base_volume(0.0)
        self.assertEqual(vol_zero, 300.0)
        print("=== 测试场景 1: 动态面值计算 (PASS) ===")

    async def test_grid_order_logic_clamping(self):
        print("\n=== 测试场景 2: 网格价格防倒挂校验 (Running) ===")
        
        entry_price = 2100.0
        atr_1h = 30.0
        atr_1d = 10.0 # Extreme case: 1D ATR < 1H ATR

        self.strategy.atr_1h_cache = atr_1h
        self.strategy.atr_1d_cache = atr_1d
        self.mock_ex.create_market_order.return_value = {'average': entry_price, 'filled': 1.0}

        await self.strategy.execute_entry(entry_price, atr_1h)

        grids = self.strategy.state.active_grids
        self.assertEqual(len(grids), 5, "应该生成 5 层网格")

        p3 = grids[2].price
        p4 = grids[3].price
        p5 = grids[4].price

        # Check L4 Clamping
        # 正常逻辑: p4 = entry - 3.0 * atr_1d = 2100 - 30 = 2070
        # 钳位逻辑: p4 < p3 (1950) - 30 = 1920
        self.assertLess(p4, p3, "L4 必须低于 L3")
        self.assertAlmostEqual(p4, p3 - 1.0*atr_1h, delta=0.1, msg="L4 未正确执行最小间距钳位")

        # Check L5 Clamping
        self.assertLess(p5, p4, "L5 必须低于 L4")
        self.assertAlmostEqual(p5, p4 - 1.0*atr_1h, delta=0.1, msg="L5 未正确执行最小间距钳位")
        print("=== 测试场景 2: 网格价格防倒挂校验 (PASS) ===")

    def test_ttp_decay_logic(self):
        print("\n=== 测试场景 3: TTP 时间衰减 (Running) ===")
        
        curr_time = time.time()
        self.strategy.state.state = TradeState.GRID_ACTIVE 

        # Case 1: Fresh Position (Held 1 hour)
        self.strategy.state.entry_timestamp = curr_time - 3600 
        activation_1 = self.strategy._get_effective_ttp_activation()
        self.assertEqual(activation_1, 1.5)

        # Case 2: Old Position (Held 80 hours)
        self.strategy.state.entry_timestamp = curr_time - (80 * 3600)
        activation_2 = self.strategy._get_effective_ttp_activation()
        self.assertEqual(activation_2, 0.2)
        print("=== 测试场景 3: TTP 时间衰减 (PASS) ===")

    def test_trend_filter_logic(self):
        print("\n=== 测试场景 4: 趋势过滤器逻辑 (Running) ===")
        self.strategy.trend_filter_enabled = True
        
        # Case 1: EMA not ready
        self.strategy._ema200_4h = None
        self.assertFalse(self.strategy.check_trend_filter(2000), "EMA未就绪应默认为 False")

        # Case 2: Uptrend (Price > EMA)
        self.strategy._ema200_4h = 1900.0
        self.assertTrue(self.strategy.check_trend_filter(2000), "价格(2000) > EMA(1900) 应为 True")

        # Case 3: Downtrend (Price < EMA)
        self.strategy._ema200_4h = 2100.0
        self.assertFalse(self.strategy.check_trend_filter(2000), "价格(2000) < EMA(2100) 应为 False")

        # Case 4: Filter Disabled
        self.strategy.trend_filter_enabled = False
        self.assertTrue(self.strategy.check_trend_filter(2000), "过滤器关闭应总是 True")
        print("=== 测试场景 4: 趋势过滤器逻辑 (PASS) ===")

    async def test_hibernation_trigger_on_crash(self):
        print("\n=== 测试场景 5: 暴跌熔断机制 (Running) ===")
        
        self.strategy.state.state = TradeState.GRID_ACTIVE
        self.strategy.state.avg_price = 2100.0  # Set avg_price to prevent division by zero
        self.strategy.t4_buffer_pct = 0.5 
        
        # 使用真实的 GridOrder 对象，避免 JSON 序列化错误
        real_grid = GridOrder(level=1, price=2000.0, amount=1.0, order_id='mock_id')
        self.strategy.state.active_grids = [real_grid]
        
        crash_price = 1989.0
        safe_price = 1991.0

        # Case 1: 价格接近但未击穿 -> 不熔断
        await self.strategy.check_grid_and_ttp(safe_price)
        self.assertFalse(self.strategy._is_frozen, "价格未击穿防线，不应冻结")

        # Case 2: 价格击穿 -> 熔断
        await self.strategy.check_grid_and_ttp(crash_price)
        self.assertTrue(self.strategy._is_frozen, "价格击穿防线，应立即冻结策略")
        self.assertEqual(self.strategy.state.state, TradeState.HIBERNATION)
        print("=== 测试场景 5: 暴跌熔断机制 (PASS) ===")

    async def test_ttp_trailing_execution(self):
        print("\n=== 测试场景 6: TTP 移动止盈全流程 (Running) ===")
        
        # Init Settings
        self.strategy.state.state = TradeState.GRID_ACTIVE
        self.strategy.state.avg_price = 2000.0
        self.strategy.state.entry_timestamp = time.time()
        self.strategy.ttp_activation_profit_pct = 1.5      
        self.strategy.ttp_trailing_loss_pct = 0.3          

        # Mock take profit execution
        self.strategy.execute_take_profit = AsyncMock()

        # Step 1: 价格 2020 (+1.0%) -> 未激活
        await self.strategy.check_grid_and_ttp(2020.0)
        self.assertEqual(self.strategy.state.state, TradeState.GRID_ACTIVE)

        # Step 2: 价格 2032 (+1.6%) -> 激活 TTP_ARMED
        await self.strategy.check_grid_and_ttp(2032.0)
        self.assertEqual(self.strategy.state.state, TradeState.TTP_ARMED)
        self.assertEqual(self.strategy.state.highest_price, 2032.0)

        # Step 3: 价格 2050 -> 更新最高价
        await self.strategy.check_grid_and_ttp(2050.0)
        self.assertEqual(self.strategy.state.highest_price, 2050.0)

        # Step 4: 价格回撤到 2045 (回撤幅度 < 0.3%)
        await self.strategy.check_grid_and_ttp(2045.0)
        self.strategy.execute_take_profit.assert_not_called()

        # Step 5: 价格砸穿 2043 -> 触发止盈
        await self.strategy.check_grid_and_ttp(2042.0)
        self.strategy.execute_take_profit.assert_called_once_with(2042.0)
        print("=== 测试场景 6: TTP 移动止盈全流程 (PASS) ===")

if __name__ == "__main__":
    import unittest
    with open("test_results.log", "w") as f:
        runner = unittest.TextTestRunner(stream=f, verbosity=2)
        unittest.main(testRunner=runner, exit=False)
