"""
bot.py - 0DTE GEX Heatmap Trading Bot
=======================================
Strategy (user's real discretionary logic):
  1. +GEX stacked nodes ($1-2 apart) = MR zone, trade between 2 biggest
  2. +GEX +DEX = SHORT (dealers sell/fade)
  3. +GEX -DEX = LONG  (dealers buy/fade)
  4. -GEX +DEX = LONG  (dealers chase up, breakout)
  5. -GEX -DEX = SHORT (dealers chase down, breakout)
  6. Track node growth: rejected at node A + node B growing = target B

Data: Tradier API (0DTE QQQ options)
Execution: MetaTrader 5 (NAS100)
"""

import sys, os, io, time, json, logging
from datetime import datetime
from collections import deque

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import builtins
_print = builtins.print
def print(*args, **kwargs):
    kwargs.setdefault("flush", True)
    _print(*args, **kwargs)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gex_engine import GEXEngine
from mt5_executor import MT5Executor
from discord_alerts import DiscordAlerts
from discord_bot import GEXBot, DayTracker

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

CONFIG = {
    # GEX
    "gex_refresh_sec": 30,          # Refresh 0DTE chain every 30s
    "proximity_qqq": 1.00,          # Entry within $1.00 of node
    "min_node_gex_pct": 0.15,       # Node must be top 15% by |GEX|
    "stack_distance": 2.0,          # Nodes within $2 = stacked

    # Signal
    "require_rejection": True,      # Wait for price rejection at node
    "rejection_bars": 3,            # Min bars near node before rejecting
    "growth_confirms_target": True, # Growing node = confirmed target

    # Risk
    "stop_buffer_qqq": 0.75,        # Stop $0.75 beyond node (in QQQ space)
    "rr_ratio": 1.5,                # Default R:R when no node target
    "max_daily_loss_usd": -500,     # Daily loss cap
    "max_positions": 1,             # Max concurrent trades
    "lot_size": 0.01,               # MT5 lot size

    # Execution
    "mt5_symbol": "NAS100",         # Broker symbol
    "cooldown_sec": 120,            # Min seconds between trades

    # Session (UTC)
    "session_start_utc": 14,        # 9:30 AM ET
    "session_end_utc": 21,          # 4:00 PM ET

    # Logging
    "log_file": "bot_trades.json",
    "print_heatmap": True,          # Print full heatmap on each refresh
    "dc_heatmap_interval": 300,      # Send heatmap to Discord every 5 min
}


# ══════════════════════════════════════════════════════════════════════════════
#  SIGNAL ENGINE — Implements user's actual strategy logic
# ══════════════════════════════════════════════════════════════════════════════

class SignalEngine:
    """
    Detects entries based on 0DTE GEX heatmap:
    - MR at +GEX stacked zones (trade between top 2 magnets)
    - Breakout at -GEX nodes
    - GEX/DEX determines direction
    - Growing nodes confirm targets
    """

    def __init__(self, config):
        self.cfg = config
        self.price_history = deque(maxlen=60)  # ~3 min at 3s ticks
        self.last_trade_time = 0
        self.state = "SCANNING"  # SCANNING → APPROACHING → ENTRY

        # Rejection tracking
        self.bars_near_node = 0
        self.approach_node = None
        self.approach_direction = None

    def tick(self, qqq_price, timestamp=None):
        """Record a price tick."""
        ts = timestamp or time.time()
        self.price_history.append({
            "price": qqq_price,
            "time": ts,
        })

    def _check_rejection(self, price, node):
        """
        Check if price is rejecting off a node:
        - Price approached node (within proximity)
        - Spent N bars near it
        - Now moving away from node
        """
        if len(self.price_history) < 6:
            return False

        dist = abs(price - node.strike)
        prox = self.cfg["proximity_qqq"]

        if dist <= prox:
            # We're near the node
            self.bars_near_node += 1

            if self.bars_near_node >= self.cfg["rejection_bars"]:
                # Check if price is now moving away
                recent = [p["price"] for p in list(self.price_history)[-4:]]
                if len(recent) >= 3:
                    if node.strike > price:
                        # Node is above — rejection = price moving down from node
                        if recent[-1] < recent[-2] < recent[-3]:
                            return True
                    else:
                        # Node is below — rejection = price moving up from node
                        if recent[-1] > recent[-2] > recent[-3]:
                            return True
        else:
            self.bars_near_node = 0

        return False

    def _find_target_node(self, gex_engine, entry_price, direction):
        """
        Find target node:
        1. If a node in the profit direction is GROWING → use that
        2. Otherwise, use nearest significant node in profit direction
        """
        growing = gex_engine.get_growing_nodes()

        # Priority: growing node in profit direction
        if self.cfg["growth_confirms_target"] and growing:
            for node in growing:
                if direction == "LONG" and node.strike > entry_price + 0.50:
                    return node
                elif direction == "SHORT" and node.strike < entry_price - 0.50:
                    return node

        # Fallback: nearest node in profit direction
        if direction == "LONG":
            above = gex_engine.get_node_above(entry_price)
            if above and abs(above.strike - entry_price) > 0.50:
                return above
        else:
            below = gex_engine.get_node_below(entry_price)
            if below and abs(below.strike - entry_price) > 0.50:
                return below

        return None

    def evaluate(self, qqq_price, gex_engine):
        """
        Main signal evaluation. Returns:
          (signal_type, entry_node, target_node, direction, stop_qqq, target_qqq)
          or (None, ...) if no signal
        """
        # Cooldown check
        if time.time() - self.last_trade_time < self.cfg["cooldown_sec"]:
            return None, None, None, None, None, None

        self.tick(qqq_price)
        nodes = gex_engine.nodes
        if not nodes:
            return None, None, None, None, None, None

        # Get significant nodes near price
        prox = self.cfg["proximity_qqq"]
        near_nodes = [n for n in nodes if abs(n.strike - qqq_price) <= prox * 2]

        if not near_nodes:
            self.state = "SCANNING"
            self.bars_near_node = 0
            return None, None, None, None, None, None

        # Find the node we're closest to
        nearest = min(near_nodes, key=lambda n: abs(n.strike - qqq_price))
        dist = abs(nearest.strike - qqq_price)

        if dist > prox:
            self.state = "SCANNING"
            self.bars_near_node = 0
            return None, None, None, None, None, None

        # We're within proximity of a node
        self.state = "APPROACHING"

        # ── DETERMINE DIRECTION FROM PRICE POSITION ────────────────────────

        if nearest.gex > 0:
            # +GEX node = MEAN REVERSION
            # Direction = fade TOWARD the node (MR)
            # Price above node → SHORT back down to node
            # Price below node → LONG back up to node
            signal_type = "MR"

            if qqq_price >= nearest.strike:
                direction = "SHORT"
            else:
                direction = "LONG"

            # Check rejection if required
            if self.cfg["require_rejection"]:
                rejected = self._check_rejection(qqq_price, nearest)
                if not rejected:
                    return None, None, None, None, None, None
                signal_type = "MR_REJECTION"
            else:
                # Just proximity + stalling
                if len(self.price_history) >= 5:
                    recent = [p["price"] for p in list(self.price_history)[-5:]]
                    rng = max(recent) - min(recent)
                    if rng > prox * 2:
                        return None, None, None, None, None, None

        else:
            # -GEX node = BREAKOUT
            # Direction = follow momentum THROUGH the node
            signal_type = "BREAKOUT"

            # Detect which way price is breaking through
            if len(self.price_history) >= 4:
                recent = [p["price"] for p in list(self.price_history)[-4:]]
                if recent[-1] > nearest.strike and recent[0] < nearest.strike:
                    direction = "LONG"   # Breaking up through -GEX
                elif recent[-1] < nearest.strike and recent[0] > nearest.strike:
                    direction = "SHORT"  # Breaking down through -GEX
                else:
                    return None, None, None, None, None, None
            else:
                return None, None, None, None, None, None

        # ── DEX CONFIDENCE ───────────────────────────────────────────────
        # DEX doesn't change direction, just adds/reduces conviction
        dex_conf = nearest.dex_confidence(direction)
        # Could use this for position sizing: higher confidence = bigger size
        # For now, log it and use as a filter for weak signals
        if dex_conf < 0.3:
            # Very low confidence — DEX strongly disagrees, skip
            return None, None, None, None, None, None

        # ── FIND TARGET ──────────────────────────────────────────────────

        target_node = self._find_target_node(gex_engine, qqq_price, direction)

        # Calculate stop and target in QQQ space
        stop_buffer = self.cfg["stop_buffer_qqq"]

        if direction == "LONG":
            stop_qqq = nearest.strike - stop_buffer
            if target_node:
                target_qqq = target_node.strike
            else:
                risk = qqq_price - stop_qqq
                target_qqq = qqq_price + risk * self.cfg["rr_ratio"]
        else:
            stop_qqq = nearest.strike + stop_buffer
            if target_node:
                target_qqq = target_node.strike
            else:
                risk = stop_qqq - qqq_price
                target_qqq = qqq_price - risk * self.cfg["rr_ratio"]

        # Validate R:R — minimum 0.5
        risk_amt = abs(qqq_price - stop_qqq)
        reward_amt = abs(target_qqq - qqq_price)
        if risk_amt <= 0 or reward_amt / risk_amt < 0.5:
            return None, None, None, None, None, None

        self.state = "ENTRY"
        self.last_trade_time = time.time()
        self.bars_near_node = 0

        return signal_type, nearest, target_node, direction, stop_qqq, target_qqq, dex_conf


# ══════════════════════════════════════════════════════════════════════════════
#  TRADE LOGGER
# ══════════════════════════════════════════════════════════════════════════════

class TradeLogger:
    def __init__(self, filepath):
        self.filepath = filepath
        self.trades = []
        self._load()

    def _load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r") as f:
                    self.trades = json.load(f)
            except:
                self.trades = []

    def _save(self):
        with open(self.filepath, "w") as f:
            json.dump(self.trades, f, indent=2, default=str)

    def log_entry(self, signal_type, direction, qqq_price, nq_price,
                  entry_node, target_node, stop_qqq, target_qqq, ticket):
        entry = {
            "time": datetime.now().isoformat(),
            "signal": signal_type,
            "direction": direction,
            "qqq_price": qqq_price,
            "nq_price": nq_price,
            "node_strike": entry_node.strike,
            "node_gex": entry_node.gex,
            "node_dex": entry_node.dex,
            "node_action": entry_node.action,
            "node_growing": entry_node.growing,
            "target_strike": target_node.strike if target_node else None,
            "stop_qqq": stop_qqq,
            "target_qqq": target_qqq,
            "ticket": ticket,
            "status": "OPEN",
        }
        self.trades.append(entry)
        self._save()
        return len(self.trades) - 1

    def log_exit(self, idx, exit_price, pnl, reason):
        if 0 <= idx < len(self.trades):
            self.trades[idx]["exit_time"] = datetime.now().isoformat()
            self.trades[idx]["exit_price"] = exit_price
            self.trades[idx]["pnl"] = pnl
            self.trades[idx]["exit_reason"] = reason
            self.trades[idx]["status"] = "CLOSED"
            self._save()

    def daily_pnl(self):
        today = datetime.now().date().isoformat()
        return sum(
            t.get("pnl", 0) for t in self.trades
            if t.get("time", "").startswith(today) and t.get("status") == "CLOSED"
        )

    def daily_trades(self):
        today = datetime.now().date().isoformat()
        return sum(1 for t in self.trades if t.get("time", "").startswith(today))


# ══════════════════════════════════════════════════════════════════════════════
#  QQQ <-> NQ CONVERSION
# ══════════════════════════════════════════════════════════════════════════════

def qqq_to_nq(qqq_level, qqq_spot, nq_price):
    """Convert QQQ price level to NQ equivalent."""
    if qqq_spot <= 0:
        return nq_price
    ratio = nq_price / qqq_spot
    return round(qqq_level * ratio, 2)


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN BOT LOOP
# ══════════════════════════════════════════════════════════════════════════════

def run_bot():
    cfg = CONFIG

    print("=" * 70)
    print("  0DTE GEX HEATMAP TRADING BOT")
    print("  Signal: Tradier (QQQ 0DTE options)")
    print("  Execution: MetaTrader 5 (NAS100)")
    print("=" * 70)
    print(f"  Strategy: MR at +GEX nodes | Breakout at -GEX nodes")
    print(f"  Direction: GEX/DEX dealer flow")
    print(f"  Proximity: ${cfg['proximity_qqq']:.2f}")
    print(f"  Stop buffer: ${cfg['stop_buffer_qqq']:.2f} beyond node")
    print(f"  Default R:R: 1:{cfg['rr_ratio']}")
    print(f"  Max positions: {cfg['max_positions']}")
    print("=" * 70)

    # Initialize
    gex = GEXEngine()
    mt5 = MT5Executor(symbol=cfg["mt5_symbol"], lot_size=cfg["lot_size"])
    signal = SignalEngine(cfg)
    logger = TradeLogger(os.path.join(os.path.dirname(__file__), cfg["log_file"]))
    dc = DiscordAlerts()
    tracker = DayTracker()

    # Start interactive Discord bot (for !levels, !status, !heatmap + alerts)
    dc_bot = GEXBot(
        gex_engine=gex,
        day_tracker=tracker,
        trade_logger=logger,
        signal_engine=signal,
        mt5_live=False,  # Updated after MT5 connect
        alerts=dc,
    )

    # Connect MT5
    print("\n[BOT] Connecting to MetaTrader 5...")
    mt5_live = mt5.connect()
    dc_bot.mt5_live = mt5_live
    if not mt5_live:
        print("[BOT] MT5 not connected - running in SIGNAL-ONLY mode")
        print("[BOT] Signals will be printed but not executed\n")

    # Start Discord command bot in background
    dc_bot.start_background()

    # Initial GEX
    print("[BOT] Loading 0DTE GEX heatmap...")
    gex.compute()
    tracker.update(gex.spot, gex.nodes)  # Track initial levels
    if cfg["print_heatmap"]:
        gex.print_heatmap()

    last_gex_refresh = time.time()
    last_heatmap_print = time.time()
    last_dc_heatmap = 0  # Send first one immediately
    active_ticket = None
    active_trade_idx = None

    # Discord startup alert
    mode = "LIVE (MT5)" if mt5_live else "SIGNAL-ONLY"
    dc.bot_status("START", f"GEX Bot started in **{mode}** mode\n"
                  f"Symbol: `{cfg['mt5_symbol']}` | Lot: `{cfg['lot_size']}`\n"
                  f"Proximity: ${cfg['proximity_qqq']:.2f} | R:R: 1:{cfg['rr_ratio']}")

    print("[BOT] Entering main loop... (Ctrl+C to stop)\n")

    try:
        while True:
            now = datetime.utcnow()
            hour = now.hour

            # ── Weekend check ───────────────────────────────────────────
            if now.weekday() >= 5:  # Saturday=5, Sunday=6
                if now.hour == 0 and now.minute == 0 and now.second < 5:
                    print(f"[BOT] Weekend. Sleeping until Monday...")
                time.sleep(300)  # 5 min sleep on weekends
                continue

            # ── Session filter ──────────────────────────────────────────
            if hour < cfg["session_start_utc"] or hour >= cfg["session_end_utc"]:
                if now.minute == 0 and now.second < 5:
                    print(f"[BOT] Outside session ({hour}:00 UTC). Waiting...")
                time.sleep(30)
                continue

            try:
                # ── Refresh GEX ─────────────────────────────────────────
                if time.time() - last_gex_refresh > cfg["gex_refresh_sec"]:
                    gex.compute()
                    tracker.update(gex.spot, gex.nodes)  # Track levels
                    last_gex_refresh = time.time()

                    # Print compact update
                    if gex.nodes:
                        nearest_above = gex.get_node_above(gex.spot)
                        nearest_below = gex.get_node_below(gex.spot)
                        growing = gex.get_growing_nodes()

                        above_str = f"${nearest_above.strike:.0f}({nearest_above.action[0]}{nearest_above.dex_bias[0]})" if nearest_above else "---"
                        below_str = f"${nearest_below.strike:.0f}({nearest_below.action[0]}{nearest_below.dex_bias[0]})" if nearest_below else "---"
                        grow_str = f" | GROWING: {','.join(f'${n.strike:.0f}' for n in growing[:3])}" if growing else ""

                        print(f"[GEX] {now:%H:%M:%S} QQQ ${gex.spot:.2f} | "
                              f"Above: {above_str} Below: {below_str}{grow_str} | "
                              f"State: {signal.state}")

                    # Full heatmap every 5 min (console)
                    if cfg["print_heatmap"] and time.time() - last_heatmap_print > 300:
                        gex.print_heatmap()
                        last_heatmap_print = time.time()

                    # Discord heatmap update
                    if time.time() - last_dc_heatmap > cfg["dc_heatmap_interval"]:
                        nearest = gex.get_nearest_nodes(gex.spot, 8)
                        nearest.sort(key=lambda n: n.strike, reverse=True)
                        dc.heatmap_update(
                            gex.spot, nearest, gex.net_gex, gex.atm_iv,
                            gex.get_growing_nodes()[:3]
                        )
                        last_dc_heatmap = time.time()

                # ── Get QQQ price ───────────────────────────────────────
                qqq_price = gex.get_spot()
                if qqq_price <= 0:
                    time.sleep(5)
                    continue

                # ── Daily loss limit ────────────────────────────────────
                daily_pnl = logger.daily_pnl()
                if daily_pnl <= cfg["max_daily_loss_usd"]:
                    print(f"[BOT] DAILY LOSS LIMIT: ${daily_pnl:.2f}. Stopping for today.")
                    # Close any open position
                    if mt5_live and active_ticket:
                        mt5.close_trade(active_ticket)
                    break

                # ── Check open position ─────────────────────────────────
                if mt5_live and active_ticket:
                    positions = mt5.get_open_positions()
                    bot_pos = [p for p in positions if p.ticket == active_ticket]
                    if not bot_pos:
                        print(f"[BOT] Position {active_ticket} closed (SL/TP hit)")
                        active_ticket = None
                        active_trade_idx = None

                # ── Max position check ──────────────────────────────────
                open_count = len(mt5.get_open_positions()) if mt5_live else (1 if active_ticket else 0)
                if open_count >= cfg["max_positions"]:
                    time.sleep(3)
                    continue

                # ── Signal evaluation ───────────────────────────────────
                result = signal.evaluate(qqq_price, gex)

                if result[0] is None:
                    time.sleep(3)
                    continue

                sig_type, entry_node, target_node, direction, stop_qqq, target_qqq, dex_conf = result

                # ── SIGNAL FIRED ────────────────────────────────────────

                risk_qqq = abs(qqq_price - stop_qqq)
                reward_qqq = abs(target_qqq - qqq_price)
                rr = reward_qqq / risk_qqq if risk_qqq > 0 else 0

                target_info = f"${target_node.strike:.0f}" if target_node else f"${target_qqq:.2f}"
                growth_tag = " [TARGET GROWING]" if (target_node and target_node.growing) else ""
                conf_bar = "!" * int(dex_conf * 10)
                dex_label = f"DEX conf: {dex_conf:.0%} {conf_bar}"

                print(f"\n{'*'*70}")
                print(f"  SIGNAL: {sig_type} | {direction} | {dex_label}")
                print(f"  Node: ${entry_node.strike:.0f} | "
                      f"GEX: {entry_node.gex:+,.0f} | DEX: {entry_node.dex:+,.0f} "
                      f"(bias: {entry_node.dex_bias})")
                print(f"  QQQ: ${qqq_price:.2f} | "
                      f"Stop: ${stop_qqq:.2f} | Target: {target_info}{growth_tag}")
                print(f"  Risk: ${risk_qqq:.2f} | Reward: ${reward_qqq:.2f} | R:R = 1:{rr:.1f}")
                print(f"{'*'*70}")

                # ── Execute on MT5 ──────────────────────────────────────
                executed = False
                ticket = None
                nq_price_exec = None

                if mt5_live:
                    nq_bid, nq_ask = mt5.get_price()
                    if nq_bid and nq_ask:
                        nq_price_exec = nq_ask if direction == "LONG" else nq_bid

                        # Convert QQQ levels to NQ
                        nq_stop = qqq_to_nq(stop_qqq, qqq_price, nq_price_exec)
                        nq_target = qqq_to_nq(target_qqq, qqq_price, nq_price_exec)

                        print(f"  NQ: {nq_price_exec:.2f} | SL: {nq_stop:.2f} | TP: {nq_target:.2f}")

                        comment = f"GEX_{entry_node.strike:.0f}_{sig_type[:2]}_{direction[0]}"
                        ticket = mt5.open_trade(direction, stop_loss=nq_stop,
                                                take_profit=nq_target, comment=comment)
                        if ticket:
                            active_ticket = ticket
                            active_trade_idx = logger.log_entry(
                                sig_type, direction, qqq_price, nq_price_exec,
                                entry_node, target_node, stop_qqq, target_qqq, ticket)
                            print(f"  [EXECUTED] Ticket: {ticket}")
                            executed = True
                        else:
                            print(f"  [FAILED] Order not filled")
                    else:
                        print(f"  [ERROR] No NQ price available")
                else:
                    # Signal-only mode
                    logger.log_entry(sig_type, direction, qqq_price, 0,
                                    entry_node, target_node, stop_qqq, target_qqq, 0)
                    print(f"  [SIGNAL ONLY] Not executed (MT5 not connected)")

                # ── Discord alert ───────────────────────────────────────
                dc.signal_alert(
                    sig_type, direction, qqq_price, entry_node,
                    target_node, stop_qqq, target_qqq, dex_conf,
                    executed=executed, ticket=ticket, nq_price=nq_price_exec
                )

                print()
                time.sleep(3)

            except Exception as e:
                print(f"[BOT] Error in loop: {e}")
                time.sleep(10)

    except KeyboardInterrupt:
        print("\n[BOT] Shutting down...")

    finally:
        # Stop Discord command bot
        dc_bot.stop_background()

        if mt5_live:
            print(f"[BOT] Open positions: {len(mt5.get_open_positions())}")
            mt5.shutdown()

        daily_pnl = logger.daily_pnl()
        daily_count = logger.daily_trades()
        print(f"[BOT] Daily P&L: ${daily_pnl:.2f}")
        print(f"[BOT] Trades today: {daily_count}")

        # Discord daily summary + shutdown
        if daily_count > 0:
            today = datetime.now().date().isoformat()
            today_trades = [t for t in logger.trades if t.get('time', '').startswith(today)]
            pnls = [t.get('pnl', 0) for t in today_trades if t.get('status') == 'CLOSED']
            wins = sum(1 for p in pnls if p > 0)
            losses = sum(1 for p in pnls if p <= 0)

            # Get final GEX node snapshot for level recap
            nearest_nodes = gex.get_nearest_nodes(gex.spot, 10) if gex.nodes else None
            if nearest_nodes:
                nearest_nodes.sort(key=lambda n: n.strike, reverse=True)

            dc.daily_summary(
                daily_count, daily_pnl, wins, losses,
                max(pnls) if pnls else 0, min(pnls) if pnls else 0,
                today_trades=today_trades,
                node_recap=nearest_nodes,
            )

        dc.bot_status("STOP", f"Daily P&L: **${daily_pnl:+,.2f}** | Trades: {daily_count}")
        dc.shutdown()
        print("[BOT] Done.")


if __name__ == "__main__":
    run_bot()
