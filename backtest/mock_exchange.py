from typing import List, Dict, Optional
import pandas as pd
import asyncio
from loguru import logger
import time
from core.exchange import BinanceExchange
from dataclasses import dataclass, field

@dataclass
class Position:
    symbol: str
    entry_price: float = 0.0
    amount: float = 0.0
    side: str = "" # LONG/SHORT, current strategy is LONG only

@dataclass
class Order:
    id: str
    symbol: str
    side: str
    type: str
    amount: float
    price: float = 0.0
    status: str = "NEW"
    filled: float = 0.0
    avg_price: float = 0.0
    timestamp: float = 0.0

class MockExchange:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.orders: Dict[str, Order] = {}
        self.position = Position(symbol)
        self.current_price = 0.0
        self.current_timestamp = 0.0
        self.order_id_counter = 1
        self.client = self # Mock client access for transfer/fetch_ohlcv if needed

    async def init_market(self, symbol, position_mode='one_way', leverage=5):
        logger.info(f"[MockExchange] Initialized for {symbol}")

    async def close(self):
        pass

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        return f"{amount:.4f}" # Simple mock precision

    def price_to_precision(self, symbol: str, price: float) -> str:
        return f"{price:.2f}" # Simple mock precision

    async def get_balance(self, asset: str = 'USDT'):
        return 10000.0 # Infinite money

    async def create_limit_order(self, symbol: str, side: str, amount: float, price: float):
        order_id = str(self.order_id_counter)
        self.order_id_counter += 1
        
        order = Order(
            id=order_id,
            symbol=symbol,
            side=side.upper(),
            type="LIMIT",
            amount=amount,
            price=price,
            timestamp=self.current_timestamp
        )
        self.orders[order_id] = order
        logger.info(f"[MockExchange] Limit Order Created: {side} {amount} @ {price} (ID: {order_id})")
        return {"id": order_id, "status": "NEW"}

    async def create_market_order(self, symbol: str, side: str, amount: float):
        order_id = str(self.order_id_counter)
        self.order_id_counter += 1
        
        # Immediate fill at current price
        fill_price = self.current_price
        
        order = Order(
            id=order_id,
            symbol=symbol,
            side=side.upper(),
            type="MARKET",
            amount=amount,
            price=0.0,
            status="FILLED",
            filled=amount,
            avg_price=fill_price,
            timestamp=self.current_timestamp
        )
        
        # Update position immediately for market orders? 
        # Strategy expects on_order_update callback, so let the runner handle updates via callbacks.
        # But we need to return filled info here as real API does.
        logger.info(f"[MockExchange] Market Order Filled: {side} {amount} @ {fill_price} (ID: {order_id})")
        return {
            "id": order_id, 
            "status": "FILLED", 
            "filled": amount, 
            "average": fill_price
        }

    async def cancel_all_orders(self, symbol: str):
        # Mark all open orders as canceled
        canceled_ids = []
        for oid, order in self.orders.items():
            if order.status == "NEW":
                order.status = "CANCELED"
                canceled_ids.append(oid)
        logger.info(f"[MockExchange] Canceled {len(canceled_ids)} orders")
        return canceled_ids

    async def get_api_entry_price(self, symbol: str) -> float:
        return self.position.entry_price

    async def fetch_ohlcv(self, symbol, timeframe, limit=100):
        # This will be monkey-patched or handled by strategy ATR cache injection
        return [] 
        
    async def transfer(self, code, amount, fromAccount, toAccount):
        logger.info(f"[MockExchange] Transfer {amount} {code} from {fromAccount} to {toAccount}")
        return True

    # Helper for backtest loop
    def match_orders(self, candle_low: float, candle_high: float) -> List[dict]:
        """Check limit orders against current candle high/low"""
        fills = []
        for oid, order in self.orders.items():
            if order.status != "NEW" or order.type != "LIMIT":
                continue
            
            # Simple matching logic
            is_match = False
            fill_price = order.price
            
            if order.side == "BUY":
                if candle_low <= order.price:
                    is_match = True
            elif order.side == "SELL":
                if candle_high >= order.price:
                    is_match = True
            
            if is_match:
                order.status = "FILLED"
                order.filled = order.amount
                order.avg_price = fill_price
                fills.append({
                    "e": "ORDER_TRADE_UPDATE",
                    "E": int(self.current_timestamp * 1000),
                    "T": int(self.current_timestamp * 1000),
                    "o": {
                        "s": self.symbol,
                        "c": "web_dummy",
                        "S": order.side,
                        "o": "LIMIT",
                        "f": "GTC",
                        "q": f"{order.amount:.4f}",
                        "p": f"{order.price:.2f}",
                        "ap": f"{fill_price:.2f}",
                        "sp": "0",
                        "x": "TRADE",
                        "X": "FILLED",
                        "i": order.id,
                        "l": f"{order.amount:.4f}",  # Last filled qty
                        "z": f"{order.amount:.4f}",  # Cumulative filled qty
                        "L": f"{fill_price:.2f}",    # Last filled price
                        "n": "0",
                        "N": "USDT",
                        "T": int(self.current_timestamp * 1000),
                        "t": 1,
                        "b": "0",
                        "a": "0",
                        "m": False,
                        "R": False,
                        "wt": "CONTRACT_PRICE",
                        "ot": "LIMIT",
                        "ps": "BOTH",
                        "cp": False,
                        "rp": "0",
                        "pP": False,
                        "si": 0,
                        "ss": 0
                    }
                })
        return fills

