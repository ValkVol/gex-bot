"""
bot.py - VWAP Deviation + HMM Regime Trading Bot
===================================================
Strategy:
  - Intraday mean-reversion / breakout at VWAP standard deviation bands
  - Anchored at Globex open (6 PM ET)
  - Mode (MR vs BO) determined by dual-layer HMM regime from Altaris
  - GEX heatmap kept for context (heatmap command + alerts)
  - Vanna flow overlay for additional confirmation

Data: Tradier API (QQQ 0DTE for GEX context), MT5 (NQ live price)
Execution: MetaTrader 5 (NAS100)
"""

import sys, os, io, time, json, math
from datetime import datetime, timedelta, timezone
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

# Also add parent dir so we can import vwap_strategy from Greek Algo root
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from gex_engine import GEXEngine
from mt5_executor import MT5Executor
from discord_alerts import DiscordAlerts
from discord_bot import GEXBot, DayTracker

# VWAP strategy components (from parent dir)
try:
    from vwap_strategy import (
        fetch_hmm_regime, DECISION_MATRIX,
        fetch_vanna_data, evaluate_vanna_overlay, apply_vanna_to_signal,
    )
    VWAP_STRATEGY_AVAILABLE = True
except ImportError as e:
    print(f"[WARN] Could not import vwap_strategy: {e}")
    VWAP_STRATEGY_AVAILABLE = False

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

CONFIG = {
    # GEX (kept for heatmap context)
    "gex_refresh_sec": 30,          # Refresh 0DTE chain every 30s
    "dc_heatmap_interval": 300,     # Send heatmap to Discord every 5 min

    # VWAP
    "vwap_bar_sec": 300,            # Build 5-minute bars from ticks
    "vwap_band_entry_threshold": 0.15,  # SD proximity to trigger entry (e.g. 1.85 SD counts as 2 SD)

    # HMM Regime
    "regime_refresh_sec": 300,      # Refresh HMM regime every 5 min
    "altaris_token": os.environ.get("ALTARIS_TOKEN", ""),

    # Risk
    "max_daily_loss_usd": -500,     # Daily loss cap
    "max_positions": 1,             # Max concurrent trades
    "lot_size": 0.01,               # MT5 lot size
    "cooldown_sec": 120,            # Min seconds between trades

    # Execution
    "mt5_symbol": "NAS100",         # Broker symbol

    # Session (UTC)
    "session_start_utc": 14,        # 9:30 AM ET
    "session_end_utc": 21,          # 4:00 PM ET

    # Logging
    "log_file": "bot_trades.json",
    "print_heatmap": True,          # Print full heatmap on each refresh
}


# ══════════════════════════════════════════════════════════════════════════════
#  LIVE VWAP TRACKER — Builds VWAP from live NQ ticks
# ══════════════════════════════════════════════════════════════════════════════

class LiveVWAPTracker:
    """
    Incremental VWAP calculator that accumulates live tick prices
    and builds N-minute bars for VWAP + SD band computation.

    Anchored at Globex open (6 PM ET = 22:00 UTC in winter, 22:00 UTC summer).
    Resets automatically at session start.

    Math:
      VWAP = Σ(TP × Volume) / Σ(Volume)
      where TP = (High + Low + Close) / 3
      Bands = VWAP ± n × σ
      σ = sqrt(Σ(Vol × (TP - VWAP)²) / Σ(Vol))
    """

    def __init__(self, bar_seconds=300):
        self.bar_seconds = bar_seconds  # Bar period (default 5 min)
        self.reset()

    def reset(self):
        """Reset for a new session."""
        self.ticks = []               # Raw ticks in current building bar
        self.bars = []                # Completed (o, h, l, c, vol) bars
        self.current_bar_start = 0    # Epoch of current bar's start
        self.session_date = None

        # Running VWAP state
        self.vwap = 0.0
        self.std_dev = 0.0
        self.bands = {}               # {1: (upper, lower), 2: ..., 4: ...}
        self.current_price = 0.0

        # Cumulative
        self._cum_tp_vol = 0.0
        self._cum_vol = 0.0
        self._cum_var = 0.0

    def tick(self, price, timestamp=None):
        """
        Feed a new price tick. Call this every few seconds.
        Automatically builds bars and updates VWAP.
        """
        ts = timestamp or time.time()
        self.current_price = price

        # Auto-reset at new session (check date change)
        today = datetime.utcnow().date()
        if self.session_date and today != self.session_date:
            # New day — reset VWAP
            print(f"[VWAP] New session detected — resetting VWAP tracker")
            self.reset()
            self.session_date = today

        if self.session_date is None:
            self.session_date = today

        # Initialize bar start
        if self.current_bar_start == 0:
            self.current_bar_start = ts

        # Add tick to current building bar
        self.ticks.append(price)

        # Check if bar is complete
        if ts - self.current_bar_start >= self.bar_seconds and len(self.ticks) >= 2:
            self._close_bar()
            self.current_bar_start = ts
            self.ticks = [price]  # Start new bar with this tick

        # Update running VWAP even mid-bar (use tick as micro-bar)
        self._update_running(price)

    def _close_bar(self):
        """Close the current bar and add to bars list."""
        if not self.ticks:
            return

        o = self.ticks[0]
        h = max(self.ticks)
        l = min(self.ticks)
        c = self.ticks[-1]
        vol = len(self.ticks)  # Tick count as volume proxy

        self.bars.append((o, h, l, c, vol))

    def _update_running(self, price):
        """
        Update running VWAP and bands from the current tick.
        Uses tick count as volume proxy (volume = 1 per tick).
        """
        tp = price  # For single tick, TP = price
        vol = 1.0

        self._cum_tp_vol += tp * vol
        self._cum_vol += vol

        if self._cum_vol > 0:
            self.vwap = self._cum_tp_vol / self._cum_vol

            # Running variance
            self._cum_var += vol * (tp - self.vwap) ** 2
            self.std_dev = math.sqrt(self._cum_var / self._cum_vol) if self._cum_vol > 1 else 0

            # Bands ±1σ through ±4σ
            for n in range(1, 5):
                self.bands[n] = (
                    round(self.vwap + n * self.std_dev, 2),
                    round(self.vwap - n * self.std_dev, 2),
                )

    def price_position(self):
        """Where is the current price relative to VWAP bands?"""
        p = self.current_price
        v = self.vwap
        sd = self.std_dev

        if sd <= 0 or v <= 0:
            return {"zone": "at_vwap", "band": 0, "side": "neutral",
                    "distance_sd": 0, "near_band": None}

        distance_sd = round((p - v) / sd, 2)
        side = "above" if p > v else "below" if p < v else "at"

        abs_dist = abs(distance_sd)
        if abs_dist < 1:
            zone = "inner"
            band = 0
        elif abs_dist < 2:
            zone = "band_1"
            band = 1
        elif abs_dist < 3:
            zone = "band_2"
            band = 2
        elif abs_dist < 4:
            zone = "band_3"
            band = 3
        else:
            zone = "band_4"
            band = 4

        # Near a specific band level? (within 0.15 SD)
        near_band = None
        for n in range(1, 5):
            upper, lower = self.bands.get(n, (0, 0))
            if sd > 0:
                if abs(p - upper) / sd < 0.15:
                    near_band = f"+{n}σ"
                elif abs(p - lower) / sd < 0.15:
                    near_band = f"-{n}σ"

        return {
            "zone": zone,
            "band": band,
            "side": side,
            "distance_sd": distance_sd,
            "near_band": near_band,
        }

    def get_levels(self):
        """Return VWAP + all band levels as dict."""
        return {
            "vwap": round(self.vwap, 2),
            "std_dev": round(self.std_dev, 2),
            "current_price": round(self.current_price, 2),
            "band_pairs": {
                n: {"upper": self.bands[n][0], "lower": self.bands[n][1]}
                for n in range(1, 5) if n in self.bands
            },
            "bar_count": len(self.bars),
            "tick_count": int(self._cum_vol),
        }

    @property
    def ready(self):
        """Need at least some data before generating signals."""
        return self._cum_vol >= 20 and self.std_dev > 0


# ══════════════════════════════════════════════════════════════════════════════
#  VWAP SIGNAL EVALUATOR — MR/BO based on VWAP position + HMM regime
# ══════════════════════════════════════════════════════════════════════════════

# Mean reversion target: move this many SDs toward the mean from entry band
TARGET_SD_MOVE = 1.5

def evaluate_vwap_signal(vwap_tracker, regime, cfg):
    """
    Evaluate VWAP signal based on live tracker position + HMM regime.

    Returns dict with:
      signal, direction, mode, entry_zone, entry_price, stop, target,
      risk_reward, size_scalar, reason
    OR signal="FLAT" if no trade.
    """
    if not vwap_tracker.ready:
        return {"signal": "FLAT", "reason": "VWAP tracker not ready (need more ticks)"}

    action = regime.get("action", "NO_TRADE")
    decision = DECISION_MATRIX.get(action, DECISION_MATRIX.get("NO_TRADE", {
        "mode": "flat", "active_bands": [], "size": 0, "description": "No trade"
    }))
    mode = decision["mode"]
    active_bands = decision["active_bands"]
    size = decision["size"]

    if mode == "flat" or not active_bands:
        return {"signal": "FLAT", "reason": decision.get("description", "Regime says no trade"),
                "mode": mode, "regime_action": action}

    pos = vwap_tracker.price_position()
    price = vwap_tracker.current_price
    sd = vwap_tracker.std_dev
    vwap = vwap_tracker.vwap

    if sd <= 0:
        return {"signal": "FLAT", "reason": "Zero standard deviation"}

    dist = pos["distance_sd"]
    threshold = cfg.get("vwap_band_entry_threshold", 0.15)

    if mode == "mean_reversion":
        return _eval_mr(vwap_tracker, active_bands, size, dist, price, sd, vwap, threshold, action)
    elif mode == "breakout":
        return _eval_bo(vwap_tracker, active_bands, size, dist, price, sd, vwap, threshold, action)

    return {"signal": "FLAT", "reason": f"Unknown mode: {mode}"}


def _eval_mr(tracker, active_bands, size, dist, price, sd, vwap, threshold, regime_action):
    """Mean reversion: fade price at outer bands back toward VWAP."""
    for band_n in sorted(active_bands):
        upper, lower = tracker.bands.get(band_n, (0, 0))

        # Check upper side: price at/above +nσ → SHORT
        if dist >= band_n - threshold:
            stop = (tracker.bands[band_n + 1][0] if band_n < 4 and (band_n + 1) in tracker.bands
                    else upper + sd * 0.5)

            target_sd = band_n - TARGET_SD_MOVE
            if target_sd <= 0:
                target = vwap
                target_label = "VWAP"
            else:
                target = vwap + target_sd * sd
                target_label = f"+{target_sd:.1f}σ"

            if target >= price:
                continue

            risk = abs(stop - price)
            reward = abs(price - target)
            rr = reward / risk if risk > 0 else 0

            return {
                "signal": "SHORT",
                "direction": "SHORT",
                "mode": "mean_reversion",
                "entry_zone": f"+{band_n}σ",
                "entry_price": round(price, 2),
                "stop": round(stop, 2),
                "target": round(target, 2),
                "target_label": target_label,
                "risk_reward": round(rr, 2),
                "size_scalar": size,
                "band_level": upper,
                "regime_action": regime_action,
                "reason": f"Price at +{band_n}σ ({upper:.2f}) — fade SHORT toward {target_label}",
            }

        # Check lower side: price at/below -nσ → LONG
        elif dist <= -(band_n - threshold):
            stop = (tracker.bands[band_n + 1][1] if band_n < 4 and (band_n + 1) in tracker.bands
                    else lower - sd * 0.5)

            target_sd = band_n - TARGET_SD_MOVE
            if target_sd <= 0:
                target = vwap
                target_label = "VWAP"
            else:
                target = vwap - target_sd * sd
                target_label = f"-{target_sd:.1f}σ"

            if target <= price:
                continue

            risk = abs(price - stop)
            reward = abs(target - price)
            rr = reward / risk if risk > 0 else 0

            return {
                "signal": "LONG",
                "direction": "LONG",
                "mode": "mean_reversion",
                "entry_zone": f"-{band_n}σ",
                "entry_price": round(price, 2),
                "stop": round(stop, 2),
                "target": round(target, 2),
                "target_label": target_label,
                "risk_reward": round(rr, 2),
                "size_scalar": size,
                "band_level": lower,
                "regime_action": regime_action,
                "reason": f"Price at -{band_n}σ ({lower:.2f}) — fade LONG toward {target_label}",
            }

    return {"signal": "FLAT", "reason": "Price not at any active MR band",
            "mode": "mean_reversion", "regime_action": regime_action}


def _eval_bo(tracker, active_bands, size, dist, price, sd, vwap, threshold, regime_action):
    """Breakout: ride momentum through VWAP bands."""
    for band_n in sorted(active_bands):
        upper, lower = tracker.bands.get(band_n, (0, 0))

        # Breaking above +nσ → LONG breakout
        if dist >= band_n - threshold and dist < band_n + 1:
            stop = (tracker.bands[band_n - 1][0] if band_n > 1 and (band_n - 1) in tracker.bands
                    else vwap)
            target = (tracker.bands[band_n + 1][0] if band_n < 4 and (band_n + 1) in tracker.bands
                      else upper + sd)

            risk = abs(price - stop)
            reward = abs(target - price)
            rr = reward / risk if risk > 0 else 0

            return {
                "signal": "LONG",
                "direction": "LONG",
                "mode": "breakout",
                "entry_zone": f"+{band_n}σ",
                "entry_price": round(price, 2),
                "stop": round(stop, 2),
                "target": round(target, 2),
                "target_label": f"+{band_n+1}σ",
                "risk_reward": round(rr, 2),
                "size_scalar": size,
                "band_level": upper,
                "regime_action": regime_action,
                "reason": f"Breaking above +{band_n}σ ({upper:.2f}) — LONG breakout toward +{band_n+1}σ",
            }

        # Breaking below -nσ → SHORT breakout
        elif dist <= -(band_n - threshold) and dist > -(band_n + 1):
            stop = (tracker.bands[band_n - 1][1] if band_n > 1 and (band_n - 1) in tracker.bands
                    else vwap)
            target = (tracker.bands[band_n + 1][1] if band_n < 4 and (band_n + 1) in tracker.bands
                      else lower - sd)

            risk = abs(stop - price)
            reward = abs(price - target)
            rr = reward / risk if risk > 0 else 0

            return {
                "signal": "SHORT",
                "direction": "SHORT",
                "mode": "breakout",
                "entry_zone": f"-{band_n}σ",
                "entry_price": round(price, 2),
                "stop": round(stop, 2),
                "target": round(target, 2),
                "target_label": f"-{band_n+1}σ",
                "risk_reward": round(rr, 2),
                "size_scalar": size,
                "band_level": lower,
                "regime_action": regime_action,
                "reason": f"Breaking below -{band_n}σ ({lower:.2f}) — SHORT breakout toward -{band_n+1}σ",
            }

    return {"signal": "FLAT", "reason": "Price not crossing any active BO band",
            "mode": "breakout", "regime_action": regime_action}


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

    def log_entry(self, signal_type, direction, nq_price, entry_zone, mode,
                  stop, target, rr, size_scalar, regime_action, ticket, vwap_at_entry):
        entry = {
            "time": datetime.now().isoformat(),
            "signal": signal_type,
            "direction": direction,
            "nq_price": nq_price,
            "entry_zone": entry_zone,
            "mode": mode,
            "stop": stop,
            "target": target,
            "rr": rr,
            "size_scalar": size_scalar,
            "regime_action": regime_action,
            "vwap_at_entry": vwap_at_entry,
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
#  MAIN BOT LOOP
# ══════════════════════════════════════════════════════════════════════════════

def run_bot():
    cfg = CONFIG

    print("=" * 70)
    print("  VWAP DEVIATION + HMM REGIME TRADING BOT")
    print("  Signal: VWAP SD Bands + Altaris HMM Regime")
    print("  Context: Tradier (QQQ 0DTE GEX heatmap)")
    print("  Execution: MetaTrader 5 (NAS100)")
    print("=" * 70)
    print(f"  Mode: Mean-Reversion / Breakout (regime-driven)")
    print(f"  Bars: {cfg['vwap_bar_sec']}s ({cfg['vwap_bar_sec']//60} min)")
    print(f"  Max positions: {cfg['max_positions']}")
    print(f"  Lot size: {cfg['lot_size']}")
    print("=" * 70)

    # Initialize components
    gex = GEXEngine()
    mt5 = MT5Executor(symbol=cfg["mt5_symbol"], lot_size=cfg["lot_size"])
    vwap = LiveVWAPTracker(bar_seconds=cfg["vwap_bar_sec"])
    logger = TradeLogger(os.path.join(os.path.dirname(__file__), cfg["log_file"]))
    dc = DiscordAlerts()
    tracker = DayTracker()

    # Start interactive Discord bot
    dc_bot = GEXBot(
        gex_engine=gex,
        day_tracker=tracker,
        trade_logger=logger,
        signal_engine=None,       # No longer using GEX SignalEngine
        mt5_live=False,
        alerts=dc,
    )
    dc_bot.vwap_tracker = vwap    # Share VWAP tracker with bot for !status

    # Connect MT5
    print("\n[BOT] Connecting to MetaTrader 5...")
    mt5_live = mt5.connect()
    dc_bot.mt5_live = mt5_live
    if not mt5_live:
        print("[BOT] MT5 not connected - running in SIGNAL-ONLY mode")
        print("[BOT] Signals will be printed but not executed\n")

    # Start Discord command bot in background
    dc_bot.start_background()

    # Initial GEX (for heatmap context)
    print("[BOT] Loading 0DTE GEX heatmap...")
    gex.compute()
    tracker.update(gex.spot, gex.nodes)
    if cfg["print_heatmap"]:
        gex.print_heatmap()

    # HMM Regime state
    regime = {"action": "NO_TRADE", "available": False, "reasoning": "Not fetched yet"}
    last_regime_fetch = 0

    last_gex_refresh = time.time()
    last_heatmap_print = time.time()
    last_dc_heatmap = 0
    last_trade_time = 0
    active_ticket = None
    active_trade_idx = None
    active_trade_info = None

    # Discord startup alert
    mode_str = "LIVE (MT5)" if mt5_live else "SIGNAL-ONLY"
    dc.bot_status("START", f"VWAP Bot started in **{mode_str}** mode\n"
                  f"Strategy: VWAP Deviation + HMM Regime\n"
                  f"Symbol: `{cfg['mt5_symbol']}` | Lot: `{cfg['lot_size']}`")

    print("[BOT] Entering main loop... (Ctrl+C to stop)\n")

    try:
        while True:
            now = datetime.utcnow()
            hour = now.hour

            # ── Weekend check ───────────────────────────────────────────
            if now.weekday() >= 5:
                if now.hour == 0 and now.minute == 0 and now.second < 5:
                    print(f"[BOT] Weekend. Sleeping until Monday...")
                time.sleep(300)
                continue

            # ── Session filter ──────────────────────────────────────────
            if hour < cfg["session_start_utc"] or hour >= cfg["session_end_utc"]:
                if now.minute == 0 and now.second < 5:
                    print(f"[BOT] Outside session ({hour}:00 UTC). Waiting...")
                time.sleep(30)
                continue

            try:
                # ── Refresh GEX (for heatmap context) ───────────────────
                if time.time() - last_gex_refresh > cfg["gex_refresh_sec"]:
                    gex.compute()
                    tracker.update(gex.spot, gex.nodes)
                    last_gex_refresh = time.time()

                    # Print compact GEX update
                    if gex.nodes:
                        nearest_above = gex.get_node_above(gex.spot)
                        nearest_below = gex.get_node_below(gex.spot)

                        above_str = f"${nearest_above.strike:.0f}({nearest_above.action[0]})" if nearest_above else "---"
                        below_str = f"${nearest_below.strike:.0f}({nearest_below.action[0]})" if nearest_below else "---"

                        print(f"[GEX] {now:%H:%M:%S} QQQ ${gex.spot:.2f} | "
                              f"Above: {above_str} Below: {below_str}")

                    # Full heatmap every 5 min
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

                # ── Get NQ price ────────────────────────────────────────
                nq_price = None
                if mt5_live:
                    nq_bid, nq_ask = mt5.get_price()
                    if nq_bid and nq_ask:
                        nq_price = (nq_bid + nq_ask) / 2.0
                else:
                    # Signal-only: derive approximate NQ from QQQ
                    qqq = gex.get_spot() if gex else 0
                    if qqq > 0:
                        nq_price = qqq * 41.5  # Rough QQQ→NQ ratio

                if not nq_price or nq_price <= 0:
                    time.sleep(5)
                    continue

                # ── Feed VWAP tracker ───────────────────────────────────
                vwap.tick(nq_price)

                # ── Update VWAP state for !status ───────────────────────
                if vwap.ready:
                    pos = vwap.price_position()
                    levels = vwap.get_levels()
                    dc_bot.vwap_state = {
                        "vwap": levels["vwap"],
                        "price": nq_price,
                        "std_dev": levels["std_dev"],
                        "distance_sd": pos["distance_sd"],
                        "zone": pos["zone"],
                        "side": pos["side"],
                        "near_band": pos.get("near_band"),
                        "bands": levels["band_pairs"],
                        "bar_count": levels["bar_count"],
                        "tick_count": levels["tick_count"],
                        "regime_action": regime.get("action", "?"),
                        "hmm_state": regime.get("hmm_state", "?"),
                        "mode": DECISION_MATRIX.get(regime.get("action", "NO_TRADE"), {}).get("mode", "flat"),
                    }

                    # Print VWAP status periodically (every 30s)
                    if int(time.time()) % 30 < 4:
                        mode_label = dc_bot.vwap_state["mode"].upper().replace("_", " ")
                        print(f"[VWAP] NQ {nq_price:,.2f} | VWAP {levels['vwap']:,.2f} | "
                              f"{pos['distance_sd']:+.2f}σ ({pos['zone']}) | "
                              f"Regime: {regime.get('action', '?')} → {mode_label}")

                # ── Refresh HMM Regime ──────────────────────────────────
                if VWAP_STRATEGY_AVAILABLE and time.time() - last_regime_fetch > cfg["regime_refresh_sec"]:
                    try:
                        new_regime = fetch_hmm_regime(cfg["altaris_token"])
                        if new_regime.get("available"):
                            old_action = regime.get("action")
                            regime = new_regime
                            if regime["action"] != old_action:
                                decision = DECISION_MATRIX.get(regime["action"], {})
                                print(f"[REGIME] {old_action} → {regime['action']} "
                                      f"({decision.get('description', '')})")
                        else:
                            print(f"[REGIME] HMM unavailable: {new_regime.get('reasoning', '?')}")
                    except Exception as e:
                        print(f"[REGIME] Fetch error: {e}")
                    last_regime_fetch = time.time()

                # ── Daily loss limit ────────────────────────────────────
                daily_pnl = logger.daily_pnl()
                if daily_pnl <= cfg["max_daily_loss_usd"]:
                    print(f"[BOT] DAILY LOSS LIMIT: ${daily_pnl:.2f}. Stopping for today.")
                    if mt5_live and active_ticket:
                        mt5.close_trade(active_ticket)
                    break

                # ── Check open position ─────────────────────────────────
                if mt5_live and active_ticket:
                    positions = mt5.get_open_positions()
                    bot_pos = [p for p in positions if p.ticket == active_ticket]
                    if not bot_pos:
                        # Position was closed (SL/TP hit or manual close)
                        print(f"[BOT] Position {active_ticket} closed (SL/TP hit)")

                        if active_trade_info:
                            entry_px = active_trade_info.get("nq_price", 0)
                            direction = active_trade_info.get("direction", "?")
                            entry_time = active_trade_info.get("entry_time")
                            entry_zone = active_trade_info.get("entry_zone", "?")
                            trade_mode = active_trade_info.get("mode", "?")

                            # Get close price
                            nq_bid, nq_ask = mt5.get_price()
                            exit_price = nq_bid or nq_ask or 0

                            # Calculate PnL
                            if direction == "LONG":
                                pnl_pts = exit_price - entry_px
                            else:
                                pnl_pts = entry_px - exit_price
                            pnl_dollar = pnl_pts * 5.0 * cfg["lot_size"]

                            # Duration
                            dur_min = 0
                            if entry_time:
                                dur_min = (datetime.now() - entry_time).total_seconds() / 60

                            # Exit reason
                            reason = "TARGET HIT" if pnl_pts > 0 else "STOP HIT"

                            # Log exit
                            if active_trade_idx is not None:
                                logger.log_exit(active_trade_idx, exit_price, pnl_dollar, reason)

                            # Discord close alert
                            dc.trade_closed(
                                direction=direction,
                                entry_price=entry_px,
                                exit_price=exit_price,
                                pnl=pnl_dollar,
                                reason=reason,
                                duration_min=dur_min,
                                entry_zone=entry_zone,
                                mode=trade_mode,
                            )

                        active_ticket = None
                        active_trade_idx = None
                        active_trade_info = None
                        dc_bot.active_trade = None

                # ── Max position check ──────────────────────────────────
                open_count = len(mt5.get_open_positions()) if mt5_live else (1 if active_ticket else 0)
                if open_count >= cfg["max_positions"]:
                    time.sleep(3)
                    continue

                # ── Cooldown check ──────────────────────────────────────
                if time.time() - last_trade_time < cfg["cooldown_sec"]:
                    time.sleep(3)
                    continue

                # ── VWAP Signal evaluation ──────────────────────────────
                if not vwap.ready or not VWAP_STRATEGY_AVAILABLE:
                    time.sleep(3)
                    continue

                trade = evaluate_vwap_signal(vwap, regime, cfg)

                if trade.get("signal") == "FLAT":
                    time.sleep(3)
                    continue

                # ── SIGNAL FIRED ────────────────────────────────────────
                direction = trade["direction"]
                entry_zone = trade["entry_zone"]
                mode = trade["mode"]
                stop = trade["stop"]
                target = trade["target"]
                rr = trade["risk_reward"]
                size_scalar = trade["size_scalar"]
                regime_action = trade.get("regime_action", "?")
                reason = trade.get("reason", "")

                mode_label = "MEAN REVERSION" if mode == "mean_reversion" else "BREAKOUT"

                print(f"\n{'*'*70}")
                print(f"  SIGNAL: {mode_label} | {direction}")
                print(f"  Zone: {entry_zone} | Regime: {regime_action}")
                print(f"  NQ: {nq_price:,.2f} | VWAP: {vwap.vwap:,.2f} | {vwap.price_position()['distance_sd']:+.2f}σ")
                print(f"  Stop: {stop:,.2f} | Target: {target:,.2f}")
                print(f"  R:R = 1:{rr:.1f} | Size: {size_scalar:.0%}")
                print(f"  {reason}")
                print(f"{'*'*70}")

                # ── Execute on MT5 ──────────────────────────────────────
                executed = False
                ticket_id = None

                if mt5_live:
                    nq_bid, nq_ask = mt5.get_price()
                    if nq_bid and nq_ask:
                        exec_price = nq_ask if direction == "LONG" else nq_bid

                        comment = f"VWAP_{entry_zone}_{mode[:2].upper()}_{direction[0]}"
                        ticket_id = mt5.open_trade(direction, stop_loss=stop,
                                                    take_profit=target, comment=comment)
                        if ticket_id:
                            active_ticket = ticket_id
                            active_trade_idx = logger.log_entry(
                                f"VWAP_{mode_label[:2]}", direction, exec_price,
                                entry_zone, mode, stop, target, rr, size_scalar,
                                regime_action, ticket_id, vwap.vwap)
                            print(f"  [EXECUTED] Ticket: {ticket_id}")
                            executed = True
                            last_trade_time = time.time()

                            # Track active trade for !status and close alerts
                            active_trade_info = {
                                "ticket": ticket_id,
                                "direction": direction,
                                "nq_price": exec_price,
                                "stop": stop,
                                "target": target,
                                "entry_zone": entry_zone,
                                "mode": mode,
                                "regime_action": regime_action,
                                "vwap_at_entry": vwap.vwap,
                                "size_scalar": size_scalar,
                                "rr": rr,
                                "entry_time": datetime.now(),
                            }
                            dc_bot.active_trade = active_trade_info
                        else:
                            print(f"  [FAILED] Order not filled")
                    else:
                        print(f"  [ERROR] No NQ price available")
                else:
                    # Signal-only mode
                    active_trade_idx = logger.log_entry(
                        f"VWAP_{mode_label[:2]}", direction, nq_price,
                        entry_zone, mode, stop, target, rr, size_scalar,
                        regime_action, 0, vwap.vwap)
                    print(f"  [SIGNAL ONLY] Not executed (MT5 not connected)")
                    last_trade_time = time.time()

                    active_trade_info = {
                        "ticket": None,
                        "direction": direction,
                        "nq_price": nq_price,
                        "stop": stop,
                        "target": target,
                        "entry_zone": entry_zone,
                        "mode": mode,
                        "regime_action": regime_action,
                        "vwap_at_entry": vwap.vwap,
                        "size_scalar": size_scalar,
                        "rr": rr,
                        "entry_time": datetime.now(),
                    }
                    dc_bot.active_trade = active_trade_info

                # ── Discord alert ───────────────────────────────────────
                dc.signal_alert(
                    direction=direction,
                    mode=mode,
                    entry_zone=entry_zone,
                    nq_price=nq_price,
                    vwap_level=vwap.vwap,
                    distance_sd=vwap.price_position()["distance_sd"],
                    stop=stop,
                    target=target,
                    target_label=trade.get("target_label", "?"),
                    rr=rr,
                    size_scalar=size_scalar,
                    regime_action=regime_action,
                    hmm_state=regime.get("hmm_state", "?"),
                    executed=executed,
                    ticket=ticket_id,
                    reason=reason,
                )

                print()
                time.sleep(3)

            except Exception as e:
                print(f"[BOT] Error in loop: {e}")
                import traceback
                traceback.print_exc()
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
