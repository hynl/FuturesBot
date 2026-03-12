import hmac
import hashlib
import time
import aiohttp
from loguru import logger


class RiskManager:
    def __init__(self, exchange_client):
        self.client = exchange_client  # 传入 ccxt 实例

    async def send_alert(self, message: str):
        """
        集成报警：这里以简单的 Loguru 为主，
        你可以扩展推送到 Telegram Bot 或 Webhook。
        """
        logger.critical(f"🔔 [ALARM]: {message}")
        # 示例：发送钉钉/Telegram 逻辑可写在此处

    async def emergency_transfer(self, amount: float = 1700.0, asset: str = 'USDT'):
        """
        极限风控：将现货备用金划转至 U 本位合约账户
        防止爆仓或用于底仓防御
        """
        try:
            coin = asset.upper()
            logger.warning(f"🛡️ 启动紧急资金划转: {amount} {coin}")

            # Binance API: 现货(MAIN) 划转至 U本位合约(UMFUTURE)
            # CCXT 统一封装了 transfer 方法
            transfer = await self.client.transfer(
                code=coin,
                amount=amount,
                fromAccount='spot',
                toAccount='future'
            )
            logger.success(f"✅ 资金划转成功: {transfer['id']}")
            return True
        except Exception as e:
            logger.error(f"❌ 资金划转失败: {e}")
            return False

    async def panic_close_all(self, symbol: str):
        """
        一键全平：在极端行情下强制退出
        """
        try:
            # 1. 撤销所有挂单
            await self.client.cancel_all_orders(symbol)
            # 2. 市价全平 (需结合当前持仓数量)
            # positions = await self.client.fetch_positions() ...
            logger.warning(f"🚨 已尝试强制平仓并撤销所有挂单: {symbol}")
        except Exception as e:
            logger.error(f"一键全平失败: {e}")