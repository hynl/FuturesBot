import ccxt.async_support as ccxt
from loguru import logger
from decimal import Decimal, ROUND_DOWN
import asyncio
from functools import wraps
from ccxt import NetworkError, ExchangeError, RateLimitExceeded

def retry_on_failure(retries=3, delay=1):
    """极客工具：自动重试装饰器"""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            for i in range(retries):
                try:
                    return await func(*args, **kwargs)
                except RateLimitExceeded as e:
                    wait = delay * (i + 1) * 2 # 指数退避
                    logger.warning(f"触发频率限制，等待 {wait}s...")
                    await asyncio.sleep(wait)
                except (NetworkError, ExchangeError) as e:
                    if i == retries - 1: raise e
                    logger.error(f"网络/交易所异常: {e}, 正在进行第 {i+1} 次重试...")
                    await asyncio.sleep(delay)
            raise Exception(f"重试 {retries} 次后仍失败")
        return wrapper
    return decorator

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

    async def init_market(self, symbol: str, position_mode: str = "one_way", leverage: int = 5):
        """初始化市场信息，并设置持仓模式与杠杆。"""
        try:
            await self.client.load_markets()

            dual_side = "false" if position_mode == "one_way" else "true"
            try:
                await self.client.fapiPrivatePostPositionSideDual({"dualSidePosition": dual_side})
            except Exception as e:
                logger.debug(f"Position side already set or error: {e}")

            market_id = self.client.market(symbol)["id"]
            try:
                resp = await self.client.fapiPrivatePostLeverage({
                    "symbol": market_id,
                    "leverage": int(leverage),
                })
                logger.info(f"Leverage configured: {market_id} -> {resp.get('leverage', leverage)}x")
            except Exception as e:
                logger.warning(f"Set leverage failed ({market_id} {leverage}x): {e}")

            logger.info("Binance Exchange Initialized.")
        except Exception as e:
            logger.error(f"Initialization Failed: {e}")
            raise

    async def close(self):
        await self.client.close()

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        """将数量转换为交易所要求的精度"""
        market = self.client.market(symbol)
        # 使用 ccxt 内置方法，它会自动根据交易所的 precision 配置处理
        return self.client.amount_to_precision(symbol, amount)

    def price_to_precision(self, symbol: str, price: float) -> str:
        """将价格转换为交易所要求的精度"""
        return self.client.price_to_precision(symbol, price)

    async def get_balance(self, asset: str = 'USDT'):
        """获取 U 本位合约可用余额"""
        balance = await self.client.fetch_balance()
        return float(balance['total'].get(asset, 0))

    @retry_on_failure(retries=5)
    async def create_limit_order(self, symbol: str, side: str, amount: float, price: float):
        """封装限价单，带精度处理"""
        precise_amount = self.amount_to_precision(symbol, amount)
        precise_price = self.price_to_precision(symbol, price)

        logger.info(f"发送限价单: {side} {precise_amount} {symbol} @ {precise_price}")
        order = await self.client.create_order(
            symbol=symbol,
            type='limit',
            side=side,
            amount=float(precise_amount),
            price=float(precise_price),
            params={'timeInForce': 'GTC'}
        )
        logger.success(f"限价单已提交: id={order['id']}")
        return order

    @retry_on_failure(retries=5)
    async def create_market_order(self, symbol: str, side: str, amount: float):
        """封装市价单，带精度处理"""
        precise_amount = self.amount_to_precision(symbol, amount)

        logger.info(f"发送市价单: {side} {precise_amount} {symbol}")
        order = await self.client.create_order(
            symbol=symbol,
            type='market',
            side=side,
            amount=float(precise_amount),
        )
        logger.success(f"市价单已提交: id={order['id']}, avg={order.get('average', '?')}")
        return order

    @retry_on_failure(retries=3)
    async def cancel_all_orders(self, symbol: str):
        """撤销指定交易对的所有未成交挂单"""
        try:
            result = await self.client.cancel_all_orders(symbol)
            logger.success(f"已撤销 {symbol} 所有挂单")
            return result
        except Exception as e:
            # 如果没有挂单，币安会返回特定错误，不应视为失败
            if 'Unknown order' in str(e) or 'No open orders' in str(e):
                logger.info(f"{symbol} 无挂单可撤")
                return []
            raise

    @retry_on_failure(retries=3)
    async def fetch_position_info(self, symbol: str) -> dict | None:
        """
        获取指定交易对的持仓信息。
        返回 ccxt 标准化的 position 字典，包含 entryPrice / contracts 等。
        """
        positions = await self.client.fetch_positions([symbol])
        for pos in positions:
            if pos['symbol'] == symbol and float(pos['contracts']) > 0:
                return pos
        return None

    async def get_api_entry_price(self, symbol: str) -> float:
        """
        从 API 获取实际持仓均价 (entryPrice)。
        策略文档要求：不能用本地公式算，必须用 API 值。
        """
        pos = await self.fetch_position_info(symbol)
        if pos and pos.get('entryPrice'):
            return float(pos['entryPrice'])
        return 0.0
