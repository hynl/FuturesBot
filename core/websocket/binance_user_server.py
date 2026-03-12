import asyncio
import json

import aiohttp
from loguru import logger


class BinanceUserStream:
    """监听个人账户变动（成交、划转、余额）"""

    def __init__(self, exchange_wrapper):
        self.ex = exchange_wrapper
        self.listen_key = None
        self.is_running = False
        self.keep_alive_task = None

    async def _stop_keep_alive(self):
        if self.keep_alive_task and not self.keep_alive_task.done():
            self.keep_alive_task.cancel()
            try:
                await self.keep_alive_task
            except asyncio.CancelledError:
                pass
        self.keep_alive_task = None

    async def keep_alive(self):
        """每 30 分钟延长一次 ListenKey 寿命"""
        try:
            while self.is_running:
                try:
                    if self.listen_key:
                        await self.ex.client.fapiPrivatePostListenKey({"listenKey": self.listen_key})
                    await asyncio.sleep(1800)
                except Exception as e:
                    logger.error(f"ListenKey 续期失败: {e}")
                    await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass
        finally:
            self.keep_alive_task = None

    async def shutdown(self):
        """停止用户流并回收后台续期任务。"""
        self.is_running = False
        await self._stop_keep_alive()

    async def subscribe_user_data(self, callback):
        """监听订单成交推送"""
        self.is_running = True
        await self._stop_keep_alive()

        res = await self.ex.client.fapiPrivatePostListenKey()
        self.listen_key = res['listenKey']
        url = f"wss://fstream.binance.com/ws/{self.listen_key}"

        # 每次订阅仅允许一个续期协程，避免重连后任务累积
        self.keep_alive_task = asyncio.create_task(self.keep_alive())

        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(url) as ws:
                    async for msg in ws:
                        if not self.is_running:
                            break
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
                            continue

                        data = json.loads(msg.data)
                        # ORDER_TRADE_UPDATE 是订单状态更新事件
                        if data.get('e') == 'ORDER_TRADE_UPDATE':
                            await callback(data['o'])
        finally:
            await self._stop_keep_alive()
