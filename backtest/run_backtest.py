import ccxt.async_support as ccxt
import pandas as pd
import pandas_ta_remake as ta
import asyncio
from loguru import logger
import os
import sys

# Ensure parent directory is in path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from strategy.eth_grid_ttp import EthGridStrategy
from core.position_manager import TradeState
from backtest.mock_exchange import MockExchange

# Backtest Configuration
CONFIG = {
    'symbol': 'ETH/USDT',
    'base_notional': 300,
    # ... other strategy settings
    'rsi_period': 14,
    'rsi_oversold': 38.0,
    'atr_period': 14,
    'trend_filter_enabled': True,
    'trend_ema_period': 200,
    # ...
    'safety_notional': 450,
    'volume_multiplier': 1.5,
    'max_safety_trades': 5,
    'grid_ratios': [1.0, 1.8, 3.0, 5.0, 8.0],
    'grid_multipliers': [1.5, 2.25, 3.375, 5.06, 7.59],
    'ttp_activation_profit_pct': 1.5,
    'ttp_trailing_loss_pct': 0.3,
    'ttp_time_decay_hours': 72.0,
    'ttp_time_decay_profit_pct': 0.2,
    't4_buffer_pct': 0.5,
    # Capital
    'reserve_capital_usdt': 1700,
}

# Load Data
def load_data():
    df_15m = pd.read_csv("backtest/eth_15m.csv", parse_dates=['datetime']).set_index('datetime'
)
    df_1h = pd.read_csv("backtest/eth_1h.csv", parse_dates=['datetime']).set_index('datetime')
    df_1d = pd.read_csv("backtest/eth_1d.csv", parse_dates=['datetime']).set_index('datetime')
    df_4h = pd.read_csv("backtest/eth_4h.csv", parse_dates=['datetime']).set_index('datetime')
    return df_15m, df_1h, df_4h, df_1d

# Pre-calculate Indicators
def calculate_indicators(df_dict):
    keys = ['15m', '1h', '4h', '1d']
    processed = {}
    for k in keys:
        df = df_dict[k].copy()
        # RSI, ATR, EMA
        df.ta.rsi(length=14, append=True)
        df.ta.atr(length=14, append=True)
        df.ta.ema(length=200, append=True)
        processed[k] = df
    return processed

async def run_backtest():
    # 强制重新添加 sink 并且指定 encoding='utf-8'，同时保留控制台输出
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    logger.add("backtest/backtest.log", rotation="10 MB", encoding="utf-8")
    
    # 1. Load Data
    logger.info("Loading Historical Data...")
    try:
        raw_dfs = {
            '15m': pd.read_csv("backtest/eth_15m.csv"),
            '1h': pd.read_csv("backtest/eth_1h.csv"),
            '4h': pd.read_csv("backtest/eth_4h.csv"),
            '1d': pd.read_csv("backtest/eth_1d.csv")
        }
    except FileNotFoundError:
        logger.error("Data not found. Please run backtest/data_loader.py first.")
        return

    # Helper to find closest index in higher timeframe DF
    def get_closes_row(target_ts, df):
        # target_ts is int timestamp ms
        row = df[df['timestamp'] <= target_ts].iloc[-1]
        return row

    # 2. Setup Mock Exchange
    mock_ex = MockExchange("ETH/USDT")
    
    # ── PnL Tracking ──
    trades_history = []  # List of {time, qty, entry_avg, exit_price, pnl, type}
    initial_balance = 10000.0
    current_balance = initial_balance
    equity_curve = []
    
    # MonkeyPatch create_market_order to track Realized PnL (SELLS)
    original_market_order = mock_ex.create_market_order
    
    async def hooked_market_order(symbol, side, amount):
        res = await original_market_order(symbol, side, amount)
        if side.upper() == 'SELL' and res['status'] == 'FILLED':
            # This is a Sell/Exit
            exit_price = float(res['average'])
            # Note: Strategy state might reset immediately after this, so we grab snapshot now if possible
            # But the strategy class holds the state.
            # Ideally strategy.state.avg_price is still valid *before* reset logic runs?
            # Strategy calls 'create_market_order', triggers fill, then updates internal state.
            # WE are intercepting the call. The strategy hasn't reset yet.
            entry_price = strategy.state.avg_price
            pnl = (exit_price - entry_price) * amount
            
            trades_history.append({
                'time': mock_ex.current_timestamp,
                'side': 'SELL',
                'qty': amount,
                'entry': entry_price,
                'exit': exit_price,
                'pnl': pnl,
                'pnl_pct': (exit_price - entry_price) / entry_price * 100
            })
            logger.success(f"💰 Realized PnL: {pnl:+.2f} USDT ({exit_price} - {entry_price})")
        return res
        
    mock_ex.create_market_order = hooked_market_order

    # 3. Setup Strategy
    strategy = EthGridStrategy(mock_ex, CONFIG)
    
    # MonkeyPatch ATR Snapshot to use our pre-loaded data
    async def mock_get_atr_snapshot(timeframe):
        current_ts = mock_ex.current_timestamp
        target_df = raw_dfs.get(timeframe)
        if target_df is None: return 0.0
        
        # Find the row that corresponds to "now"
        # Since we are iterating 15m candles, for 1H ATR we want the last closed 1H candle
        mask = target_df['timestamp'] <= current_ts
        if not mask.any(): return 0.0
        
        # Calculate ATR on the fly for this window or use pre-calc?
        # Strategy usually calculates on windowed data.
        # Let's simplify: slice the dataframe up to current_ts and calc ATR on last row
        relevant_df = target_df.loc[mask].tail(30).copy() # last 20 rows enough for ATR 14? need prev close
        relevant_df.ta.atr(length=14, append=True)
        val = relevant_df[f"ATRr_14"].iloc[-1]
        return float(val) if pd.notna(val) else 0.0

    strategy.get_atr_snapshot = mock_get_atr_snapshot

    # 4. Main Loop
    df_15m = raw_dfs['15m']
    total_candles = len(df_15m)
    
    logger.info(f"Starting Backtest on {total_candles} candles...")
    
    # Pre-warm loop needed? Strategy builds indicators on window.
    # We pass window of data to on_kline_closed (e.g. 100 rows)
    window_size = 200
    
    # ── Tracking PnL ──
    realized_pnl = 0.0
    trade_count = 0
    
    for i in range(window_size, total_candles):
        current_row = df_15m.iloc[i]
        current_ts = current_row['timestamp']
        mock_ex.current_timestamp = current_ts
        # For display only, close price is the "current" price at end of loop
        mock_ex.current_price = current_row['close'] 
        
        # 4a. Update Trends (4H EMA)
        # Find relevant 4H candle
        df_4h = raw_dfs['4h']
        mask_4h = df_4h['timestamp'] <= current_ts
        if mask_4h.any():
            relevant_4h = df_4h.loc[mask_4h].tail(210).copy()
            relevant_4h.ta.ema(length=200, append=True)
            if 'EMA_200' in relevant_4h.columns:
                ema_val = relevant_4h['EMA_200'].iloc[-1]
                if pd.notna(ema_val):
                    strategy.update_trend_ema(float(ema_val))

        # 4b. Mock Real-time Price Stream (Check TTP / Grids)
        # ─── Robust Simulation Logic ───
        # Determine path based on candle color to simulate "Real" movement
        o, h, l, c = current_row['open'], current_row['high'], current_row['low'], current_row['close']
        
        # Green Candle (Close >= Open): Open -> Low -> High -> Close (Dip then Rip)
        # Red Candle   (Close < Open):  Open -> High -> Low -> Close (pump then Dump)
        if c >= o:
            price_path = [o, l, h, c]
        else:
            price_path = [o, h, l, c]
            
        for price_step in price_path:
            mock_ex.current_price = price_step # Price moves...
            
            # Step 1: Check Limit Order Fills (Grid Buys)
            # Important: Process limit fills BEFORE strategy checks, so strategy sees updated position
            fills = []
            # specific logic: We only fill BUY orders if price <= limit
            # Note: In real engine, we iterate NEW orders. 
            # We copy items() to allow modification during iteration if needed (though we just modify status)
            for oid, order in list(mock_ex.orders.items()):
                if order.status == "NEW" and order.type == "LIMIT" and order.side == "BUY":
                    if price_step <= order.price:
                        # FILLED!
                        fill_qty = order.amount
                        fill_cost = fill_qty * order.price # Keep it simple, buy at limit price
                        
                        fill_data = {
                            'X': 'FILLED',
                            'S': 'BUY',
                            'L': order.price, # Price
                            'l': fill_qty,    # Qty
                            'i': str(oid),
                            'T': current_ts
                        }
                        order.status = "FILLED"
                        order.avg_price = order.price
                        fills.append(fill_data)
                        logger.info(f"⚡ [Sim Match] Grid Buy Filled: {fill_qty} @ {order.price}")

            # Step 2: Push fills to strategy
            for fill in fills:
                await strategy.on_order_update(fill)
                
            # Step 3: Check TTP (Sell) or Defense Breach
            # capture previous state to detect PnL changes
            # NOTE: Strategy executes MARKET SELLS for TTP. 
            # We need to hook into the 'create_market_order' of mock_ex to capture Realized PnL?
            # Or reliance on strategy state changes.
            # Strategy calls 'create_market_order' -> MockExchange returns fill -> Strategy state updates avg_price/size.
            # We can track 'realized_pnl' by watching strategy resets or order history?
            # Let's just track Total Equity change? 
            # Simplest: Watch strategy.execute_take_profit call? No, too intrusive.
            # We will interpret 'market sell' from MockExchange logs or we can calc it ourselves.
            
            await strategy.check_grid_and_ttp(price_step)

        # 4d. Kline Closed
        window_df = df_15m.iloc[i-window_size+1 : i+1].copy()
        await strategy.on_kline_closed(window_df)
        
        # ─── Monitoring & Logging ───
        if i % 50 == 0:
            # Calculate Floating PnL
            holding_cost = strategy.state.avg_price * strategy.state.total_amount
            current_val = mock_ex.current_price * strategy.state.total_amount
            unrealized = current_val - holding_cost if strategy.state.total_amount > 0 else 0.0
            
            # Estimate Realized? 
            # We can just look at mock_ex balance? MockEx has infinite money.
            # Let's rely on internal state logs.
            
            logger.info(
                f"📊 Progress: {i}/{total_candles} | "
                f"Price: {mock_ex.current_price:.2f} | "
                f"State: {strategy.state.state.name} | "
                f"Pos: {strategy.state.total_amount:.4f} ETH @ {strategy.state.avg_price:.2f} | "
                f"Unrealized PnL: {unrealized:+.2f} U"
            )

    # ─── Final Summary ───
    logger.info("\n" + "="*50)
    logger.info("🏁 BACKTEST RESULTS SUMMARY")
    logger.info("="*50)
    
    total_realized_pnl = sum(t['pnl'] for t in trades_history)
    win_trades = [t for t in trades_history if t['pnl'] > 0]
    loss_trades = [t for t in trades_history if t['pnl'] <= 0]
    win_rate = len(win_trades) / len(trades_history) * 100 if trades_history else 0.0
    
    # Calculate Float PnL
    holding_pnl = 0.0
    if strategy.state.total_amount > 0:
        curr_price = df_15m.iloc[-1]['close']
        holding_pnl = (curr_price - strategy.state.avg_price) * strategy.state.total_amount
    
    final_equity = initial_balance + total_realized_pnl + holding_pnl
    
    res = f"""
    Time Range: {df_15m.iloc[0]['datetime']}  ->  {df_15m.iloc[-1]['datetime']}
    Duration  : {len(df_15m)} 15m candles
    ---------------------------
    Total Trades    : {len(trades_history)}
    Win Rate        : {win_rate:.1f}% ({len(win_trades)} W / {len(loss_trades)} L)
    ---------------------------
    Realized PnL    : {total_realized_pnl:+.2f} USDT
    Floating PnL    : {holding_pnl:+.2f} USDT (Open Pos: {strategy.state.total_amount:.4f} ETH)
    Final Equity    : {final_equity:.2f} USDT (Start: {initial_balance:.2f})
    Return %        : {(final_equity - initial_balance)/initial_balance*100:+.2f}%
    """
    logger.info(res)
    
    # Save trade list
    if trades_history:
        pd.DataFrame(trades_history).to_csv("backtest/trades.csv", index=False)
        logger.info("📝 Trade history saved to backtest/trades.csv")

    logger.success("Backtest Completed.")

if __name__ == "__main__":
    if not os.path.exists("backtest"):
        os.makedirs("backtest")
    # First ensure data exists (user needs to run data_loader.py)
    asyncio.run(run_backtest())
