"""
mt5_executor.py - MetaTrader 5 Trade Execution
================================================
Handles order placement, position management, and account monitoring.
Connects to a running MT5 terminal on the local machine.
"""

import os
import time
from datetime import datetime

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False
    print("[MT5] MetaTrader5 package not installed. Run: pip install MetaTrader5")


class MT5Executor:
    def __init__(self, symbol="NAS100", lot_size=0.01, magic=777777):
        """
        symbol: instrument name in your MT5 broker (NAS100, USTEC, NQ, etc.)
        lot_size: default position size
        magic: magic number for identifying bot trades
        """
        self.symbol = symbol
        self.lot_size = lot_size
        self.magic = magic
        self.connected = False

    def connect(self):
        """Initialize MT5 connection."""
        if not MT5_AVAILABLE:
            print("[MT5] MetaTrader5 package not available")
            return False

        if not mt5.initialize():
            print(f"[MT5] Failed to initialize: {mt5.last_error()}")
            return False

        info = mt5.account_info()
        if info is None:
            print("[MT5] No account info - check MT5 is logged in")
            return False

        print(f"[MT5] Connected to {info.server}")
        print(f"[MT5] Account: {info.login} | Balance: ${info.balance:,.2f} | Leverage: 1:{info.leverage}")

        # Verify symbol exists
        sym_info = mt5.symbol_info(self.symbol)
        if sym_info is None:
            print(f"[MT5] Symbol '{self.symbol}' not found. Trying alternatives...")
            # Try common NAS100 names
            for alt in ["NAS100", "USTEC", "US100", "NQ", "NSDQ100", "NAS100.cash"]:
                sym_info = mt5.symbol_info(alt)
                if sym_info is not None:
                    self.symbol = alt
                    print(f"[MT5] Found '{alt}' - using this symbol")
                    break

            if sym_info is None:
                print("[MT5] Could not find NAS100 symbol. Available symbols:")
                symbols = mt5.symbols_get()
                nasdaq = [s.name for s in symbols if "NAS" in s.name.upper()
                          or "US100" in s.name.upper() or "USTEC" in s.name.upper()
                          or "NDX" in s.name.upper() or "NQ" in s.name.upper()]
                print(f"  Matching: {nasdaq[:10]}")
                return False

        # Enable symbol in market watch
        if not sym_info.visible:
            mt5.symbol_select(self.symbol, True)

        tick = mt5.symbol_info_tick(self.symbol)
        if tick:
            print(f"[MT5] {self.symbol}: Bid={tick.bid} Ask={tick.ask} Spread={tick.ask - tick.bid:.1f}")

        print(f"[MT5] Point: {sym_info.point} | Min lot: {sym_info.volume_min} | Max lot: {sym_info.volume_max}")
        self.connected = True
        return True

    def get_price(self):
        """Get current bid/ask for the symbol."""
        if not self.connected:
            return None, None
        tick = mt5.symbol_info_tick(self.symbol)
        if tick:
            return tick.bid, tick.ask
        return None, None

    def open_trade(self, direction, lot_size=None, stop_loss=None, take_profit=None, comment="GEX"):
        """
        Place a market order.
        direction: 'LONG' or 'SHORT'
        Returns: order ticket or None
        """
        if not self.connected:
            print("[MT5] Not connected")
            return None

        lots = lot_size or self.lot_size
        tick = mt5.symbol_info_tick(self.symbol)
        if not tick:
            print("[MT5] Cannot get price")
            return None

        if direction == "LONG":
            order_type = mt5.ORDER_TYPE_BUY
            price = tick.ask
        else:
            order_type = mt5.ORDER_TYPE_SELL
            price = tick.bid

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": lots,
            "type": order_type,
            "price": price,
            "sl": stop_loss or 0.0,
            "tp": take_profit or 0.0,
            "deviation": 20,  # slippage tolerance in points
            "magic": self.magic,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result is None:
            print(f"[MT5] Order send returned None: {mt5.last_error()}")
            return None

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            print(f"[MT5] Order failed: {result.retcode} - {result.comment}")
            return None

        print(f"[MT5] {direction} {lots} {self.symbol} @ {result.price} | "
              f"SL: {stop_loss or 'None'} TP: {take_profit or 'None'} | "
              f"Ticket: {result.order}")

        return result.order

    def close_trade(self, ticket=None, comment="GEX_CLOSE"):
        """Close a specific position by ticket, or close all bot positions."""
        if not self.connected:
            return False

        if ticket:
            positions = mt5.positions_get(ticket=ticket)
        else:
            positions = mt5.positions_get(symbol=self.symbol)

        if not positions:
            print("[MT5] No positions to close")
            return False

        closed = 0
        for pos in positions:
            if pos.magic != self.magic and ticket is None:
                continue  # Skip non-bot positions

            tick = mt5.symbol_info_tick(self.symbol)
            if not tick:
                continue

            if pos.type == mt5.ORDER_TYPE_BUY:
                close_type = mt5.ORDER_TYPE_SELL
                price = tick.bid
            else:
                close_type = mt5.ORDER_TYPE_BUY
                price = tick.ask

            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": self.symbol,
                "volume": pos.volume,
                "type": close_type,
                "position": pos.ticket,
                "price": price,
                "deviation": 20,
                "magic": self.magic,
                "comment": comment,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }

            result = mt5.order_send(request)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                pnl = pos.profit
                print(f"[MT5] Closed ticket {pos.ticket} | PnL: ${pnl:.2f}")
                closed += 1
            else:
                err = result.comment if result else mt5.last_error()
                print(f"[MT5] Close failed for {pos.ticket}: {err}")

        return closed > 0

    def modify_sl_tp(self, ticket, stop_loss=None, take_profit=None):
        """Modify SL/TP on an open position."""
        if not self.connected:
            return False

        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            print(f"[MT5] Position {ticket} not found")
            return False

        pos = positions[0]
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": self.symbol,
            "position": ticket,
            "sl": stop_loss if stop_loss is not None else pos.sl,
            "tp": take_profit if take_profit is not None else pos.tp,
        }

        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            print(f"[MT5] Modified {ticket}: SL={stop_loss} TP={take_profit}")
            return True
        else:
            err = result.comment if result else mt5.last_error()
            print(f"[MT5] Modify failed: {err}")
            return False

    def get_open_positions(self):
        """Get all bot positions."""
        if not self.connected:
            return []
        positions = mt5.positions_get(symbol=self.symbol)
        if not positions:
            return []
        return [p for p in positions if p.magic == self.magic]

    def get_account_info(self):
        """Get account balance and equity."""
        if not self.connected:
            return {}
        info = mt5.account_info()
        if info:
            return {
                "balance": info.balance,
                "equity": info.equity,
                "margin": info.margin,
                "free_margin": info.margin_free,
                "profit": info.profit,
            }
        return {}

    def shutdown(self):
        """Cleanly disconnect from MT5."""
        if MT5_AVAILABLE:
            mt5.shutdown()
            self.connected = False
            print("[MT5] Disconnected")


if __name__ == "__main__":
    ex = MT5Executor(symbol="NAS100", lot_size=0.01)
    if ex.connect():
        bid, ask = ex.get_price()
        print(f"\nCurrent price: Bid={bid} Ask={ask}")
        acc = ex.get_account_info()
        print(f"Account: {acc}")
        positions = ex.get_open_positions()
        print(f"Open positions: {len(positions)}")
        ex.shutdown()
