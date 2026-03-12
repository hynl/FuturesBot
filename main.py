import pandas as pd
from core.exchange import BinanceExchange
from core.position_manager import TradeState
from core.websocket.binance_user_server import BinanceUserStream
from core.websocket.binance_ws_server import BinanceWSServer
from strategy.eth_grid_ttp import EthGridStrategy
from utils.metrics import TradeMetrics
from utils.config_watcher import ConfigWatcher
import time
import asyncio
from loguru import logger
import os
from pathlib import Path
from dotenv import load_dotenv
import yaml

# 获取当前脚本所在目录，然后相对地找到 config/secrets.env
BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / "config" / "secrets.env"
SETTINGS_FILE = BASE_DIR / "config" / "settings.yaml"
load_dotenv(ENV_FILE)


def build_um_symbol(base_asset: str, quote_asset: str) -> str:
    """Build a Binance UM futures symbol like ETHUSDT / ETHUSDC."""
    base = (base_asset or "ETH").upper().strip()
    quote = (quote_asset or "USDT").upper().strip()
    if quote not in {"USDT", "USDC"}:
        raise ValueError(f"Unsupported quote asset for UM futures: {quote}")
    return f"{base}{quote}"


def _parse_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_int(value, default: int, min_value: int = 1) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError) as e:
        raise ValueError(f"Invalid int value: {value}") from e
    if parsed < min_value:
        raise ValueError(f"Value must be >= {min_value}, got {parsed}")
    return parsed


def _parse_float(value, default: float, min_value: float = 0.0) -> float:
    try:
        parsed = float(value) if value is not None else default
    except (TypeError, ValueError) as e:
        raise ValueError(f"Invalid float value: {value}") from e
    if parsed < min_value:
        raise ValueError(f"Value must be >= {min_value}, got {parsed}")
    return parsed


def load_settings_config() -> dict:
    if not SETTINGS_FILE.exists():
        raise FileNotFoundError(f"settings.yaml not found: {SETTINGS_FILE}")

    with SETTINGS_FILE.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError("settings.yaml root must be a mapping object")
    return data


def load_runtime_config() -> dict:
    settings = load_settings_config()
    trade_cfg = settings.get("trade", {})
    runtime_cfg = settings.get("runtime", {})
    health_cfg = settings.get("health", {})
    position_cfg = settings.get("position", {})
    capital_cfg = settings.get("capital", {})
    safety_cfg = settings.get("safety", {})
    rsi_cfg = settings.get("rsi", {})
    atr_cfg = settings.get("atr", {})
    grid_cfg = settings.get("grid", {})
    ttp_cfg = settings.get("ttp", {})
    trend_cfg = settings.get("trend_filter", {})
    sizer_cfg = settings.get("dynamic_sizer", {})
    t4_cfg = settings.get("t4_breach", {})

    base_asset = str(trade_cfg.get("base_asset", "ETH")).upper().strip()
    quote_asset = str(trade_cfg.get("quote_asset", "USDT")).upper().strip()
    derived_symbol = build_um_symbol(base_asset, quote_asset)
    explicit_symbol = str(trade_cfg.get("symbol", "")).upper().strip()
    symbol = explicit_symbol or derived_symbol

    volume_multiplier = _parse_float(safety_cfg.get("volume_multiplier"), 1.5, min_value=1.0)
    max_safety_trades = _parse_int(safety_cfg.get("max_trades"), 4, min_value=1)
    default_multipliers = [round(volume_multiplier ** (i + 1), 6) for i in range(max_safety_trades)]
    default_ratios = [1.0, 1.8, 3.0, 5.0][:max_safety_trades]

    return {
        "api_key": os.getenv("BINANCE_API_KEY"),
        "secret": os.getenv("BINANCE_SECRET"),
        "base_asset": base_asset,
        "quote_asset": quote_asset,
        "derived_symbol": derived_symbol,
        "symbol": symbol,
        "base_notional": _parse_float(trade_cfg.get("base_notional"), 300.0, min_value=1.0),
        "position_mode": str(position_cfg.get("mode", "one_way")).lower().strip(),
        "leverage": _parse_int(position_cfg.get("leverage"), 5, min_value=1),
        "running_capital_usdt": _parse_float(capital_cfg.get("running_pool_usdt"), 800.0, min_value=0.0),
        "reserve_capital_usdt": _parse_float(capital_cfg.get("reserve_spot_usdt"), 1700.0, min_value=0.0),
        "safety_notional": _parse_float(safety_cfg.get("order_value"), 450.0, min_value=1.0),
        "volume_multiplier": volume_multiplier,
        "max_safety_trades": max_safety_trades,
        "testnet": _parse_bool(runtime_cfg.get("testnet"), True),
        "interval": str(runtime_cfg.get("kline_interval", "15m")),
        "interval_4h": str(runtime_cfg.get("kline_interval_4h", "4h")),
        "ohlcv_limit": _parse_int(runtime_cfg.get("ohlcv_limit"), 100, min_value=20),
        "ohlcv_limit_4h": _parse_int(runtime_cfg.get("ohlcv_limit_4h"), 210, min_value=201),
        "price_poll_seconds": _parse_float(runtime_cfg.get("price_poll_seconds"), 1.0, min_value=0.1),
        "health_timeout_seconds": _parse_int(health_cfg.get("timeout_seconds"), 60, min_value=10),
        "health_check_seconds": _parse_int(health_cfg.get("check_seconds"), 30, min_value=5),
        "rsi_period": _parse_int(rsi_cfg.get("period"), 14, min_value=2),
        "rsi_oversold": _parse_float(rsi_cfg.get("oversold"), 40.0, min_value=0.0),
        "rsi_overbought": _parse_float(rsi_cfg.get("overbought"), 70.0, min_value=0.0),
        "atr_period": _parse_int(atr_cfg.get("period"), 14, min_value=2),
        "grid_ratios": grid_cfg.get("ratios", default_ratios),
        "grid_multipliers": grid_cfg.get("multipliers", default_multipliers),
        "ttp_activation_profit_pct": _parse_float(ttp_cfg.get("activation_profit_pct"), 1.5, min_value=0.1),
        "ttp_trailing_loss_pct": _parse_float(ttp_cfg.get("trailing_loss_pct"), 0.3, min_value=0.01),
        # V2.0 新增
        "trend_filter_enabled": _parse_bool(trend_cfg.get("enabled"), True),
        "trend_ema_period": _parse_int(trend_cfg.get("ema_period"), 200, min_value=1),
        "baseline_atr": _parse_float(sizer_cfg.get("baseline_atr"), 15.0, min_value=0.1),
        "dynamic_sizer_min": _parse_float(sizer_cfg.get("min_notional"), 150.0, min_value=1.0),
        "dynamic_sizer_max": _parse_float(sizer_cfg.get("max_notional"), 450.0, min_value=1.0),
        "ttp_time_decay_hours": _parse_float(ttp_cfg.get("time_decay_hours"), 72.0, min_value=1.0),
        "ttp_time_decay_profit_pct": _parse_float(ttp_cfg.get("time_decay_profit_pct"), 0.2, min_value=0.01),
        "t4_buffer_pct": _parse_float(t4_cfg.get("buffer_pct"), 0.5, min_value=0.0),
    }


def validate_config(config: dict) -> None:
    if not config.get("api_key") or config["api_key"] == "YOUR_API_KEY":
        raise ValueError("BINANCE_API_KEY is missing or still placeholder value")
    if not config.get("secret") or config["secret"] == "YOUR_SECRET":
        raise ValueError("BINANCE_SECRET is missing or still placeholder value")
    if config["symbol"] != config["derived_symbol"]:
        raise ValueError("trade.symbol must match base_asset + quote_asset")
    if config["position_mode"] != "one_way":
        raise ValueError("position.mode must be one_way")
    if len(config["grid_ratios"]) != config["max_safety_trades"]:
        raise ValueError("grid.ratios length must equal safety.max_trades")
    if len(config["grid_multipliers"]) != config["max_safety_trades"]:
        raise ValueError("grid.multipliers length must equal safety.max_trades")


CONFIG = load_runtime_config()

class TradingBot:
    def __init__(self):
        self.ex = BinanceExchange(CONFIG['api_key'], CONFIG['secret'], CONFIG['testnet'])
        self.ws = BinanceWSServer(CONFIG['symbol'], interval=CONFIG['interval'])
        self.user_ws = BinanceUserStream(self.ex)
        self.metrics = TradeMetrics(CONFIG['symbol'])
        self.strategy = EthGridStrategy(
            self.ex, CONFIG,
            metrics_callback=self.metrics.record_trade,
            hibernation_callback=self._enter_hibernation_loop,
        )
        self.config_watcher = ConfigWatcher(str(SETTINGS_FILE))
        self.config_watcher.on_config_changed(self._on_config_update)
        # 1. 定义：记录最后一次收到有效数据的时间戳 (系统启动即初始化)
        self.last_msg_time = time.time()
        self.is_shutting_down = False
        self.is_hibernating = False
        self.worker_tasks = []

    async def _on_config_update(self, new_config: dict):
        """配置文件变更回调，触发策略热更新"""
        try:
            await self.strategy.update_parameters(new_config)
        except Exception as e:
            logger.error(f"策略参数热更新失败: {e}")

    async def _enter_hibernation_loop(self):
        """
        策略触发 HIBERNATION 后的死循环。
        停止所有 worker，仅维持心跳日志，直到人工重启进程。
        """
        self.is_hibernating = True
        self.is_shutting_down = True  # 让所有 while 循环自然退出

        # 取消所有后台任务
        for task in self.worker_tasks:
            if not task.done():
                task.cancel()
        # 不 await gather，直接进入死循环

        logger.critical("💀 主程序进入 HIBERNATION 死循环, 仅维持心跳日志. 请人工重启进程!")
        while True:
            logger.critical("💀 HIBERNATION 心跳: 进程存活, 等待人工介入...")
            await asyncio.sleep(86400)  # 24小时

    async def fetch_historical_klines(self):
        """启动时预加载历史K线，用于计算初始指标"""
        logger.info("正在获取历史数据以计算指标...")
        # 获取指定周期的历史K线
        ohlcv = await self.ex.client.fetch_ohlcv(
            CONFIG['symbol'],
            timeframe=CONFIG['interval'],
            limit=CONFIG['ohlcv_limit']
        )
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        return df

    async def fetch_4h_ema200(self) -> float | None:
        """获取 4H K线并计算 EMA200，用于趋势过滤"""
        try:
            ohlcv = await self.ex.client.fetch_ohlcv(
                CONFIG['symbol'],
                timeframe=CONFIG['interval_4h'],
                limit=CONFIG['ohlcv_limit_4h']
            )
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            ema_col = df.ta.ema(length=CONFIG['trend_ema_period'], append=True)
            ema_val = df[f"EMA_{CONFIG['trend_ema_period']}"].iloc[-1]
            if pd.notna(ema_val):
                return float(ema_val)
            return None
        except Exception as e:
            logger.error(f"获取4H EMA200失败: {e}")
            return None

    async def trend_update_worker(self):
        """轨道 6: 定期刷新 4H EMA200 并注入策略"""
        logger.info("📈 4H EMA200 趋势更新 Worker 已启动")
        while not self.is_shutting_down:
            try:
                ema_val = await self.fetch_4h_ema200()
                self.strategy.update_trend_ema(ema_val)
                if ema_val is not None:
                    logger.info(f"📈 4H EMA200 更新: {ema_val:.2f}")
                # 每 15 分钟刷新一次 (与 K 线周期对齐)
                await asyncio.sleep(900)
            except Exception as e:
                if self.is_shutting_down:
                    break
                logger.error(f"Trend Update Error: {e}")
                await asyncio.sleep(60)

    async def kline_closed_worker(self):
        """
        轨道 1: 监听 K 线闭合
        由 WebSocket 的 'kline_closed' 信号触发
        """

        async def on_kline_callback(data):
            # 2a. 喂狗：只要 WebSocket 收到任何 K 线数据，就刷新时间戳
            self.last_msg_time = time.time()
            # 当 K 线闭合时，重新拉取完整的历史数据确保指标准确
            df = await self.fetch_historical_klines()
            await self.strategy.on_kline_closed(df)

        while not self.is_shutting_down:
            try:
                await self.ws.subscribe(on_kline_callback)
                if not self.is_shutting_down:
                    logger.warning("Kline WS 已断开，5秒后尝试重连...")
                    await asyncio.sleep(5)
            except Exception as e:
                if self.is_shutting_down:
                    break
                logger.error(f"Kline Worker Error: {e}")
                await asyncio.sleep(5)

    async def real_time_price_worker(self):
        """
        轨道 2: 毫秒级实时价格监控
        用于驱动 TTP 和网格逻辑
        """
        while not self.is_shutting_down:
            try:
                # 生产环境建议用 WS 价格，这里先用 REST 轮询演示逻辑
                ticker = await self.ex.client.fetch_ticker(CONFIG['symbol'])
                curr_price = ticker['last']

                # 2b. 喂狗：只要成功获取到最新价格，就刷新时间戳
                self.last_msg_time = time.time()

                await self.strategy.check_grid_and_ttp(curr_price)
                await asyncio.sleep(CONFIG['price_poll_seconds'])
            except Exception as e:
                if self.is_shutting_down:
                    break
                logger.error(f"Price Worker Error: {e}")
                await asyncio.sleep(5)

    async def init_state_and_sync(self):
        """启动时的对账逻辑"""
        # 1. 尝试从磁盘读取
        loaded = self.strategy.state.load_from_disk()

        # 2. 从交易所获取实际仓位
        positions = await self.ex.client.fetch_positions([CONFIG['symbol']])
        remote_pos = next((p for p in positions if p['symbol'] == CONFIG['symbol']), None)

        remote_qty = float(remote_pos['contracts']) if remote_pos else 0.0

        if remote_qty > 0:
            logger.info(f"检测到实盘存在仓位: {remote_qty}")
            if not loaded:
                logger.warning("本地无备份，尝试根据实盘持仓逆向恢复状态...")
                # 逆向推导：如果没有本地记录但有仓位，我们认为它处于 GRID_WAITING
                self.strategy.state.state = TradeState.GRID_ACTIVE
                self.strategy.state.total_amount = remote_qty
                self.strategy.state.avg_price = float(remote_pos['entryPrice'])
                # 注意：此时 ATR 和 RSI 相关的网格挂单需要通过 fetch_open_orders 重新补全
        else:
            if loaded and self.strategy.state.state != TradeState.HUNTING:
                logger.warning("本地显示有仓位但交易所为空，状态重置。")
                self.strategy.state.reset()

    async def monitor_health(self):
        """
        轨道 3: 看门狗健康自检 (Watchdog)
        这是极其关键的守护协程，确保系统不会进入“假死”状态
        """
        logger.info("🛡️ 看门狗 (Watchdog) 健康监控已启动...")
        timeout_threshold = CONFIG['health_timeout_seconds']
        health_check_seconds = CONFIG['health_check_seconds']

        while not self.is_shutting_down:
            current_time = time.time()
            time_since_last_msg = current_time - self.last_msg_time

            # 3. 巡查：如果超过阈值没有收到任何数据
            if time_since_last_msg > timeout_threshold:
                logger.critical(f"🚨 严重警告: 超过 {time_since_last_msg:.1f} 秒未收到数据，连接可能已假死！")

                # ==== 自愈逻辑 (Self-Healing) ====
                # 方案 A: 强制断开 WebSocket 让其内部机制重连
                self.ws.stop()

                # 方案 B: 触发风控报警 (结合之前写的 RiskManager)
                # risk_manager = RiskManager(self.ex.client)
                # await risk_manager.send_alert("服务器行情流断开，正在尝试自愈！")

                # 重置时间戳，给予系统重连的缓冲时间，避免下一秒继续疯狂报警
                self.last_msg_time = time.time()

                # 每轮巡查间隔可配置
            await asyncio.sleep(health_check_seconds)

     async def user_stream_worker(self):
         """轨道 4: 监听账户订单更新并回调策略状态机。"""

         async def on_order_callback(order_data):
             self.last_msg_time = time.time()
             await self.strategy.on_order_update(order_data)

         while not self.is_shutting_down:
             try:
                 await self.user_ws.subscribe_user_data(on_order_callback)
                 if not self.is_shutting_down:
                     logger.warning("User Stream 已断开，5秒后尝试重连...")
                     await asyncio.sleep(5)
             except Exception as e:
                 if self.is_shutting_down:
                     break
                 logger.error(f"User Stream Worker Error: {e}")
                 await asyncio.sleep(5)

+    async def config_watcher_worker(self):
+        """轨道 5: 配置文件监听，支持热更新"""
+        await self.config_watcher.start()

     async def shutdown(self):
        """优雅退出：停止WS并关闭交易所连接。"""
        if self.is_shutting_down:
            return
        self.is_shutting_down = True
        logger.info("开始优雅退出，正在停止后台任务...")
        self.metrics.print_stats()

        for task in self.worker_tasks:
            if not task.done():
                task.cancel()
        if self.worker_tasks:
            await asyncio.gather(*self.worker_tasks, return_exceptions=True)

        await self.user_ws.shutdown()
        self.ws.stop()
        try:
            await self.ex.close()
        except Exception as e:
            logger.warning(f"关闭交易所连接时出现异常: {e}")
        logger.info("优雅退出完成。")

    async def run(self):
        try:
            # 1. 初始化交易所连接
            await self.ex.init_market(
                symbol=CONFIG['symbol'],
                position_mode=CONFIG['position_mode'],
                leverage=CONFIG['leverage'],
            )
            await self.init_state_and_sync()

            # 1.5 预热 4H EMA200 趋势数据
            ema_val = await self.fetch_4h_ema200()
            self.strategy.update_trend_ema(ema_val)
            logger.info(f"📈 4H EMA200 预热完成: {ema_val}")

            # 2. 启动并发任务
            logger.success("🤖 交易机器人已上线，开始监听市场...")
            self.worker_tasks = [
                asyncio.create_task(self.kline_closed_worker(), name="kline_closed_worker"),
                asyncio.create_task(self.real_time_price_worker(), name="real_time_price_worker"),
                asyncio.create_task(self.monitor_health(), name="monitor_health"),
                asyncio.create_task(self.user_stream_worker(), name="user_stream_worker"),
                asyncio.create_task(self.config_watcher_worker(), name="config_watcher_worker"),
                asyncio.create_task(self.trend_update_worker(), name="trend_update_worker"),
            ]

            done, _ = await asyncio.wait(self.worker_tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task.cancelled():
                    if not self.is_shutting_down:
                        logger.warning(f"Worker {task.get_name()} 被取消，触发系统停机。")
                    continue

                exc = task.exception()
                if exc:
                    logger.error(f"Worker {task.get_name()} 异常退出: {exc}")
                elif not self.is_shutting_down:
                    logger.error(f"Worker {task.get_name()} 意外结束，触发系统停机。")
        finally:
            await self.shutdown()


if __name__ == "__main__":
    try:
        validate_config(CONFIG)
        logger.info(
            "启动配置: symbol={} mode={} leverage={}x interval={} testnet={} poll={}s timeout={}s".format(
                CONFIG['symbol'],
                CONFIG['position_mode'],
                CONFIG['leverage'],
                CONFIG['interval'],
                CONFIG['testnet'],
                CONFIG['price_poll_seconds'],
                CONFIG['health_timeout_seconds'],
            )
        )
        logger.info(
            "资金配置: futures_pool={} reserve_spot={} first_order={} safety_order={} max_safety={} volume_multiplier={}".format(
                CONFIG['running_capital_usdt'],
                CONFIG['reserve_capital_usdt'],
                CONFIG['base_notional'],
                CONFIG['safety_notional'],
                CONFIG['max_safety_trades'],
                CONFIG['volume_multiplier'],
            )
        )
        logger.info(
            "策略配置: RSI周期={} 超卖={} ATR周期={} 网格{}层 TTP激活={}% 回撤={}%".format(
                CONFIG['rsi_period'],
                CONFIG['rsi_oversold'],
                CONFIG['atr_period'],
                len(CONFIG['grid_ratios']),
                CONFIG['ttp_activation_profit_pct'],
                CONFIG['ttp_trailing_loss_pct'],
            )
        )
        bot = TradingBot()
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("用户停止机器人")
    except Exception as e:
        logger.error(f"启动失败: {e}")
