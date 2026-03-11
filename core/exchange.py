import ccxt.async_support as ccxt
from loguru import logger


class BinanceExchange:
    def __init__(self, api_key: str, secret: str, testnet: bool = False):
        self.client = ccxt.binance({
            'apiKey': api_key,
            'secret': secret,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'future',  # 默认 U 本位合约
                'recvWindow': 5000,
            }
        })
        if testnet:
            self.client.set_sandbox_mode(True)

    async def init_market(self):
        """初始化市场信息及设置持仓模式"""
        try:
            await self.client.load_markets()
            # 强制设置为单向持仓模式 (One-way Mode)
            # 注意：币安 API 修改持仓模式可能因已有仓位报错，需捕获异常
            try:
                await self.client.fapiPrivatePostPositionSideDual({"dualSidePosition": "false"})
            except Exception as e:
                logger.debug(f"Position side already set or error: {e}")

            logger.info("Binance Exchange Initialized.")
        except Exception as e:
            logger.error(f"Initialization Failed: {e}")
            raise

    async def close(self):
        await self.client.close()