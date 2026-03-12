import asyncio
import json
import aiohttp
from loguru import logger

class BinanceWSServer:
    """监听币安 K 线数据，支持自动重连"""
    def __init__(self, symbol: str, interval: str = '15m'):
        self.url = f"wss://fstream.binance.com/ws/{symbol.lower()}@kline_{interval}"
        self.is_running = False

    async def subscribe(self, callback):
        """
        callback: 接收数据的异步函数
        """
        self.is_running = True
        while self.is_running:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(self.url) as ws:
                        logger.info(f"Connected to Binance WS: {self.url}")
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                data = json.loads(msg.data)
                                # 如果 K 线闭合 (k['x'] == True)
                                if data.get('k', {}).get('x'):
                                    await callback(data)
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
            except Exception as e:
                logger.warning(f"WS Connection lost: {e}. Retrying in 5s...")
                await asyncio.sleep(5)

    def stop(self):
        self.is_running = False