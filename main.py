import asyncio
import pandas as pd
from loguru import logger
from core.exchange import BinanceExchange
from core.websocket import BinanceWSServer
from strategy.eth_grid_ttp import EthGridStrategy

# 模拟配置加载
CONFIG = {
    "api_key": "YOUR_API_KEY",
    "secret": "YOUR_SECRET",
    "symbol": "ETHUSDT",
    "base_notional": 300,  # 300U 首单面值
    "testnet": True
}


class TradingBot:
    def __init__(self):
        self.ex = BinanceExchange(CONFIG['api_key'], CONFIG['secret'], CONFIG['testnet'])
        self.ws = BinanceWSServer(CONFIG['symbol'], interval='15m')
        self.strategy = EthGridStrategy(self.ex, CONFIG)

    async def fetch_historical_klines(self):
        """启动时预加载历史K线，用于计算初始指标"""
        logger.info("正在获取历史数据以计算指标...")
        # 获取最近 100 条 15m K线
        ohlcv = await self.ex.client.fetch_ohlcv(CONFIG['symbol'], timeframe='15m', limit=100)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        return df

    async def kline_closed_worker(self):
        """
        轨道 1: 监听 K 线闭合
        由 WebSocket 的 'kline_closed' 信号触发
        """

        async def on_kline_callback(data):
            # 当 K 线闭合时，重新拉取完整的历史数据确保指标准确
            df = await self.fetch_historical_klines()
            await self.strategy.on_kline_closed(df)

        await self.ws.subscribe(on_kline_callback)

    async def real_time_price_worker(self):
        """
        轨道 2: 毫秒级实时价格监控
        用于驱动 TTP 和网格逻辑
        """
        while True:
            try:
                # 生产环境建议用 WS 价格，这里先用 REST 轮询演示逻辑
                ticker = await self.ex.client.fetch_ticker(CONFIG['symbol'])
                curr_price = ticker['last']
                await self.strategy.check_grid_and_ttp(curr_price)
                await asyncio.sleep(1)  # 1秒轮询一次，WS 模式下可更快
            except Exception as e:
                logger.error(f"Price Worker Error: {e}")
                await asyncio.sleep(5)

    async def run(self):
        # 1. 初始化交易所连接
        await self.ex.init_market()

        # 2. 启动并发任务
        logger.success("🤖 交易机器人已上线，开始监听市场...")
        await asyncio.gather(
            self.kline_closed_worker(),
            self.real_time_price_worker()
        )


if __name__ == "__main__":
    bot = TradingBot()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("用户停止机器人")