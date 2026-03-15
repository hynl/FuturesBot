import ccxt.async_support as ccxt
import pandas as pd
import asyncio
from loguru import logger
import os
from dotenv import load_dotenv
from pathlib import Path

# Load environment variables from config/secrets.env
BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / "config" / "secrets.env"
if ENV_FILE.exists():
    load_dotenv(ENV_FILE)

async def download_data(symbol: str, timeframe: str, start_date: str, end_date: str, filename: str):
    # Configure CCXT with optional proxy support
    exchange_config = {
        'enableRateLimit': True,
        'timeout': 30000,
    }
    
    # Check for proxy in environment
    proxy = os.getenv("HTTPS_PROXY") or os.getenv("http_proxy") or os.getenv("https_proxy")
    if proxy:
        logger.info(f"Using proxy: {proxy}")
        exchange_config['proxies'] = {
            'http': proxy,
            'https': proxy,
        }
        exchange_config['aiohttp_proxy'] = proxy # Helper for some ccxt versions
    else:
        logger.warning(f"No proxy detected. Attempting direct connection for {symbol}...")

    exchange = None
    try:
        exchange = ccxt.binanceusdm(exchange_config)
        logger.info(f"Connecting to Binance via {proxy if proxy else 'Direct'}...")
        await exchange.load_markets()
        logger.info(f"Successfully loaded {len(exchange.markets)} markets from Binance.")
        
        # 自动纠正交易对格式: 如果 ETH/USDT 不存在，尝试找 ETH/USDT:USDT
        if symbol not in exchange.markets:
            correction = f"{symbol}:USDT"
            if correction in exchange.markets:
                logger.warning(f"⚠️ Symbol {symbol} mapped to {correction} for data fetching.")
                symbol = correction
            else:
                logger.error(f"⚠️ Symbol {symbol} not found! Available ETH symbols:")
                eth_symbols = [m for m in exchange.markets if "ETH" in m]
                logger.error(f"{eth_symbols[:10]} ...")
                return

        since = exchange.parse8601(start_date)
        end_ts = exchange.parse8601(end_date)
        
        all_ohlcv = []
        
        logger.info(f"Start downloading {symbol} {timeframe} from {start_date} to {end_date}")
        
        while since < end_ts:
            ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, since, limit=1000)
            if not ohlcv:
                break
            
            last_ts = ohlcv[-1][0]
            if last_ts == since:
                break
                
            all_ohlcv.extend(ohlcv)
            since = last_ts + 1
            logger.info(f"Fetching... Current: {exchange.iso8601(last_ts)}")
            await asyncio.sleep(0.5) # Rate limit
            
        df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.to_csv(filename, index=False)
        logger.success(f"Saved {len(df)} rows to {filename}")
        
    except Exception as e:
        logger.error(f"Download error: {e}")
        if "451" in str(e) or "Geo-blocked" in str(e) or "Service unavailable" in str(e):
             logger.critical("🛑 您的代理 IP 可能位于受限地区（如美国/新加坡）。请切换到 日本/台湾/香港 节点再试。")
        elif "403" in str(e):
             logger.critical("🛑 403 Forbidden: IP 被封禁或 Cloudflare 拦截。请更换节点。")
        elif "ClientConnectorError" in str(e) or "TimeoutError" in str(e):
             logger.warning("Network connection failed. Please check your proxy settings in config/secrets.env (e.g., HTTPS_PROXY=http://127.0.0.1:7890)")
    finally:
        if exchange:
            await exchange.close()

async def main():
    # 2024-08-05 Crash Event Backtest Data
    # We download surrounding data: 2024-07-25 to 2024-08-15
    start_date = "2026-03-13T00:00:00Z"
    end_date = "2026-03-15T00:00:00Z"
    
    tasks = [
        download_data("ETH/USDT", "15m", start_date, end_date, "backtest/eth_15m.csv"),
        download_data("ETH/USDT", "1h", start_date, end_date, "backtest/eth_1h.csv"),
        download_data("ETH/USDT", "4h", start_date, end_date, "backtest/eth_4h.csv"),
        download_data("ETH/USDT", "1d", start_date, end_date, "backtest/eth_1d.csv"),
    ]
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    if not os.path.exists("backtest"):
        os.makedirs("backtest")
    asyncio.run(main())
