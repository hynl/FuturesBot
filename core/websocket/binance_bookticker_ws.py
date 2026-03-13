"""
Binance @bookTicker WebSocket
─────────────────────────────
订阅单交易对的实时最优买卖价 (Best Bid/Ask)，
用于替代 REST fetch_ticker 轮询（策略审查清单 #4）。

优势:
  - 零 API 权重消耗 (WebSocket 是免费的)
  - 毫秒级延迟 (vs REST ~200ms)
  - 不会触发频率限制
"""

import asyncio
import json
import time
from typing import Callable

import aiohttp
from loguru import logger


class BinanceBookTickerWS:
    """
    订阅 fstream @bookTicker，推送实时最优价格。
    支持自动重连 + 超时检测 (策略审查清单 #2)。
    """

    def __init__(self, symbol: str, timeout_seconds: float = 30.0):
        self.symbol = symbol.lower()
        self.url = f"wss://fstream.binance.com/ws/{self.symbol}@bookTicker"
        self.timeout_seconds = timeout_seconds
        self.is_running = False
        self.last_price: float = 0.0
        self.last_update_time: float = 0.0

    async def subscribe(self, callback: Callable):
        """
        持久订阅 bookTicker，收到数据后调用 callback(price: float)。
        内置超时重连（解决策略审查清单 #2 假死问题）。

        callback: async def on_price(price: float) -> None
        """
        self.is_running = True
        while self.is_running:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(
                        self.url,
                        heartbeat=20,  # aiohttp 自动 Ping/Pong
                    ) as ws:
                        logger.info(f"📡 BookTicker WS 已连接: {self.url}")
                        while self.is_running:
                            try:
                                msg = await asyncio.wait_for(
                                    ws.receive(),
                                    timeout=self.timeout_seconds,
                                )
                            except asyncio.TimeoutError:
                                logger.warning(
                                    f"BookTicker WS 超时 {self.timeout_seconds}s 无数据, 断开重连..."
                                )
                                break

                            if msg.type == aiohttp.WSMsgType.TEXT:
                                data = json.loads(msg.data)
                                # bookTicker: {'u':id, 's':'ETHUSDT', 'b':'3000.00', 'B':'1.5', 'a':'3000.10', 'A':'2.0'}
                                best_bid = float(data.get('b', 0))
                                best_ask = float(data.get('a', 0))
                                mid_price = (best_bid + best_ask) / 2.0

                                self.last_price = mid_price
                                self.last_update_time = time.time()

                                await callback(mid_price)

                            elif msg.type in (
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.CLOSING,
                                aiohttp.WSMsgType.ERROR,
                            ):
                                logger.warning(f"BookTicker WS 收到关闭信号: {msg.type}")
                                break

            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self.is_running:
                    break
                logger.warning(f"BookTicker WS 异常: {e}, 3秒后重连...")
                await asyncio.sleep(3)

    def stop(self):
        self.is_running = False

