"""
discord_bot.py - Interactive Discord Bot with Commands
=======================================================
Runs alongside the main trading loop in a background thread.
Responds to commands:
  !levels   - VWAP bands + GEX nodes
  !status   - Bot status, VWAP position, active trade info
  !heatmap  - Current GEX heatmap snapshot
  !vwap     - VWAP deviation strategy overview + backtest stats

Requires DISCORD_BOT_TOKEN in .env
"""

import os
import threading
import asyncio
from datetime import datetime
from collections import OrderedDict

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")


class DayTracker:
    """
    Tracks all GEX nodes seen throughout the trading day.
    Preserves historical levels even after they fall off the active list.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        """Reset for a new trading day."""
        self.date = datetime.now().date()
        # strike -> {first_seen, last_seen, gex_history, max_gex, type, action, ...}
        self.nodes_seen = OrderedDict()
        self.snapshots = []  # [(timestamp, spot, [node_summaries])]

    def update(self, spot, nodes):
        """Record a GEX snapshot. Call this on every GEX refresh."""
        now = datetime.now()

        # Auto-reset on new day
        if now.date() != self.date:
            self.reset()

        for n in nodes:
            key = n.strike
            if key not in self.nodes_seen:
                self.nodes_seen[key] = {
                    "strike": n.strike,
                    "first_seen": now,
                    "last_seen": now,
                    "first_gex": n.gex,
                    "last_gex": n.gex,
                    "max_gex": abs(n.gex),
                    "min_gex": abs(n.gex),
                    "type": n.type,
                    "action": n.action,
                    "dex_bias": n.dex_bias,
                    "gex_sign": "+" if n.gex > 0 else "-",
                    "times_seen": 1,
                    "was_growing": n.growing,
                    "last_dex": n.dex,
                }
            else:
                entry = self.nodes_seen[key]
                entry["last_seen"] = now
                entry["last_gex"] = n.gex
                entry["max_gex"] = max(entry["max_gex"], abs(n.gex))
                entry["min_gex"] = min(entry["min_gex"], abs(n.gex))
                entry["type"] = n.type
                entry["action"] = n.action
                entry["dex_bias"] = n.dex_bias
                entry["gex_sign"] = "+" if n.gex > 0 else "-"
                entry["times_seen"] += 1
                entry["last_dex"] = n.dex
                if n.growing:
                    entry["was_growing"] = True

        # Save snapshot (keep last 200 = ~100 min at 30s refresh)
        snap = [(n.strike, n.gex, n.dex, n.action, n.growing) for n in nodes[:12]]
        self.snapshots.append((now, spot, snap))
        if len(self.snapshots) > 200:
            self.snapshots.pop(0)

    def get_current_strikes(self, nodes):
        """Get set of currently active strikes."""
        return {n.strike for n in nodes} if nodes else set()

    def get_all_levels(self, current_nodes=None):
        """
        Get all levels seen today, split into active vs historical.
        Returns (active_levels, historical_levels) sorted by strike desc.
        """
        current_strikes = self.get_current_strikes(current_nodes) if current_nodes else set()

        active = []
        historical = []

        for strike, info in self.nodes_seen.items():
            entry = {**info}
            duration = (entry["last_seen"] - entry["first_seen"]).total_seconds()
            entry["duration_min"] = duration / 60

            # GEX change from first to last observation
            if entry["first_gex"] != 0:
                entry["gex_change_pct"] = (
                    (entry["last_gex"] - entry["first_gex"]) / abs(entry["first_gex"]) * 100
                )
            else:
                entry["gex_change_pct"] = 0

            if strike in current_strikes:
                active.append(entry)
            else:
                historical.append(entry)

        active.sort(key=lambda x: x["strike"], reverse=True)
        historical.sort(key=lambda x: x["strike"], reverse=True)

        return active, historical


class GEXBot(commands.Bot):
    """Discord bot with VWAP + GEX commands. Shares state with the trading loop."""

    def __init__(self, gex_engine=None, day_tracker=None, trade_logger=None,
                 signal_engine=None, mt5_live=False, alerts=None):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

        self.gex = gex_engine
        self.tracker = day_tracker
        self.logger = trade_logger
        self.signal_engine = signal_engine
        self.mt5_live = mt5_live
        self.alerts = alerts
        self.active_trade = None      # Set by bot.py when a trade is opened/closed
        self.vwap_tracker = None      # Set by bot.py — LiveVWAPTracker instance
        self.vwap_state = None        # Set by bot.py on each tick — dict with VWAP info
        self._thread = None
        self._loop = None
        self._ready_event = threading.Event()

        # Register commands using closures (discord.py 2.3+ compatible)
        bot_ref = self

        @self.command(name="levels")
        async def levels(ctx):
            await bot_ref._cmd_levels(ctx)

        @self.command(name="status")
        async def status(ctx):
            await bot_ref._cmd_status(ctx)

        @self.command(name="heatmap")
        async def heatmap(ctx):
            await bot_ref._cmd_heatmap(ctx)

        @self.command(name="vwap")
        async def vwap(ctx):
            await bot_ref._cmd_vwap(ctx)

    async def on_ready(self):
        print(f"[DC-BOT] Logged in as {self.user} (ID: {self.user.id})")

        # Find first text channel we can send to
        alert_channel = None
        for guild in self.guilds:
            for ch in guild.text_channels:
                if ch.permissions_for(guild.me).send_messages:
                    alert_channel = ch
                    break
            if alert_channel:
                break

        # Share connection with DiscordAlerts
        if self.alerts and alert_channel:
            self.alerts.attach(self, self._loop)
            self.alerts.set_channel(alert_channel)

        self._ready_event.set()

    # ── !levels ─────────────────────────────────────────────────────────
    async def _cmd_levels(self, ctx):
        """Show VWAP bands + active GEX levels."""
        parts = []

        # ── VWAP Bands ──
        if self.vwap_state:
            vs = self.vwap_state
            price = vs.get("price", 0)
            vwap_val = vs.get("vwap", 0)
            dist = vs.get("distance_sd", 0)
            bands = vs.get("bands", {})

            vwap_lines = []
            vwap_lines.append(f"**VWAP:** `{vwap_val:,.2f}` | **NQ:** `{price:,.2f}` | **{dist:+.2f}σ** ({vs.get('side', '?')})")
            vwap_lines.append("")

            # Band table
            vwap_lines.append(f"{'Band':<8} {'Lower':>12} {'Upper':>12}")
            vwap_lines.append(f"{'─'*36}")

            for n in range(1, 5):
                bp = bands.get(n, bands.get(str(n), {}))
                lower = bp.get("lower", 0)
                upper = bp.get("upper", 0)

                # Mark current price position
                marker = ""
                if abs(dist) >= n - 0.15 and abs(dist) < n + 0.85:
                    marker = " ◀"

                vwap_lines.append(f"`±{n}σ`    `{lower:>12,.2f}` `{upper:>12,.2f}`{marker}")

            # Regime info
            regime = vs.get("regime_action", "?")
            mode = vs.get("mode", "?").upper().replace("_", " ")
            hmm = vs.get("hmm_state", "?")
            vwap_lines.append(f"\n**Regime:** {regime} → {mode} | HMM: {hmm}")

            parts.append("**📐 VWAP Bands**\n" + "\n".join(vwap_lines))
        else:
            parts.append("**📐 VWAP Bands**\n*VWAP tracker not ready yet*")

        # ── GEX Nodes (context) ──
        if self.gex and self.tracker:
            active, _ = self.tracker.get_all_levels(self.gex.nodes)
            spot = self.gex.spot

            if active:
                gex_lines = []
                for lv in active[:8]:
                    gex_sign = lv["gex_sign"]
                    dist_qqq = lv["strike"] - spot
                    dist_str = f"{dist_qqq:+.2f}" if dist_qqq != 0 else "ATM"
                    spot_marker = " ◀" if abs(dist_qqq) < 0.50 else ""

                    gex_lines.append(
                        f"`${lv['strike']:>6.0f}` {lv['type'][:3]} | "
                        f"{gex_sign}GEX `{lv['last_gex']:>+13,.0f}` | "
                        f"{lv['action']} → {lv['dex_bias']}{spot_marker}"
                    )

                parts.append(f"\n**🗺️ GEX Context** (QQQ ${spot:.2f})\n" + "\n".join(gex_lines))

        desc = "\n".join(parts)
        if len(desc) > 3900:
            desc = desc[:3900] + "\n..."

        embed = discord.Embed(
            title=f"📊 Levels — {datetime.now():%b %d %H:%M}",
            description=desc,
            color=0x5865F2,
            timestamp=datetime.utcnow(),
        )

        await ctx.send(embed=embed)

    # ── !status ─────────────────────────────────────────────────────────
    async def _cmd_status(self, ctx):
        """Show current bot status, VWAP position, active trade, and state."""
        mode = "🟢 LIVE" if self.mt5_live else "🔵 SIGNAL-ONLY"

        daily_pnl = self.logger.daily_pnl() if self.logger else 0
        daily_trades = self.logger.daily_trades() if self.logger else 0

        embed = discord.Embed(
            title="📊 Bot Status",
            color=0x00FF88 if daily_pnl >= 0 else 0xFF4444,
            timestamp=datetime.utcnow(),
        )
        embed.add_field(name="Mode", value=mode, inline=True)
        embed.add_field(name="Daily P&L", value=f"**${daily_pnl:+,.2f}**", inline=True)
        embed.add_field(name="Trades Today", value=f"{daily_trades}", inline=True)

        # ── VWAP Position ──
        if self.vwap_state:
            vs = self.vwap_state
            price = vs.get("price", 0)
            vwap_val = vs.get("vwap", 0)
            dist = vs.get("distance_sd", 0)
            zone = vs.get("zone", "?")
            regime = vs.get("regime_action", "?")
            hmm = vs.get("hmm_state", "?")
            mode_str = vs.get("mode", "flat").upper().replace("_", " ")
            near = vs.get("near_band", "")

            position_text = (
                f"NQ: **{price:,.2f}** | VWAP: **{vwap_val:,.2f}**\n"
                f"Position: **{dist:+.2f}σ** ({zone})"
            )
            if near:
                position_text += f" — near **{near}**"

            embed.add_field(name="📐 VWAP Position", value=position_text, inline=False)

            regime_text = f"**{regime}** → {mode_str} | HMM: {hmm}"
            embed.add_field(name="🧠 Regime", value=regime_text, inline=False)
        else:
            embed.add_field(name="📐 VWAP", value="*Tracker warming up...*", inline=False)

        # ── Active Trade Info ──
        if self.active_trade:
            t = self.active_trade
            direction = t.get("direction", "?")
            dir_emoji = "🟢" if direction == "LONG" else "🔴"
            entry_zone = t.get("entry_zone", "?")
            trade_mode = t.get("mode", "?")
            mode_label = "MR" if trade_mode == "mean_reversion" else "BO" if trade_mode == "breakout" else trade_mode
            entry_time = t.get("entry_time")

            # Duration
            dur_str = "—"
            if entry_time:
                elapsed = (datetime.now() - entry_time).total_seconds()
                dur_min = int(elapsed // 60)
                dur_sec = int(elapsed % 60)
                dur_str = f"{dur_min}m {dur_sec}s"

            trade_lines = []
            trade_lines.append(f"{dir_emoji} **{direction}** | **{mode_label}** at **{entry_zone}**")
            trade_lines.append(f"Entry: **{t.get('nq_price', 0):,.2f}** | VWAP: `{t.get('vwap_at_entry', 0):,.2f}`")
            trade_lines.append(f"Stop: `{t.get('stop', 0):,.2f}` | Target: `{t.get('target', 0):,.2f}`")
            trade_lines.append(f"R:R = **1:{t.get('rr', 0):.1f}** | Size: **{t.get('size_scalar', 0):.0%}**")
            trade_lines.append(f"Regime: {t.get('regime_action', '?')} | Duration: **{dur_str}**")

            if t.get("ticket"):
                trade_lines.append(f"Ticket: `{t['ticket']}`")

            embed.add_field(
                name="🔥 ACTIVE TRADE",
                value="\n".join(trade_lines),
                inline=False,
            )
        else:
            embed.add_field(
                name="📋 Position",
                value="*No active trade — scanning for setups*",
                inline=False,
            )

        if self.vwap_state:
            embed.set_footer(text=f"Ticks: {self.vwap_state.get('tick_count', 0)} | "
                                  f"Bars: {self.vwap_state.get('bar_count', 0)}")

        await ctx.send(embed=embed)

    # ── !heatmap ────────────────────────────────────────────────────────
    async def _cmd_heatmap(self, ctx):
        """Show current GEX heatmap snapshot."""
        if not self.gex or not self.gex.nodes:
            await ctx.send("⚠️ No GEX data loaded yet.")
            return

        spot = self.gex.spot
        nearest = self.gex.get_nearest_nodes(spot, 12)
        nearest.sort(key=lambda n: n.strike, reverse=True)

        max_gex = max(abs(n.gex) for n in nearest) if nearest else 1

        lines = []
        for n in nearest:
            bar_len = int(abs(n.gex) / max_gex * 15)
            bar = ("🟩" if n.gex > 0 else "🟥") * bar_len

            growth = " 📈" if n.growing else (" 📉" if n.shrinking else "")
            spot_marker = " ◀" if abs(n.strike - spot) < 0.50 else ""
            dir_arrow = "⬆" if n.dex_bias == "LONG" else "⬇"

            lines.append(
                f"`${n.strike:>6.0f}` {n.type[:3]} | "
                f"`{'+' if n.gex > 0 else ''}{n.gex:>12,.0f}` | "
                f"{n.action} {dir_arrow} {bar}{growth}{spot_marker}"
            )

        growing = self.gex.get_growing_nodes()
        grow_str = ""
        if growing:
            grow_str = "\n\n**Growing Nodes:**\n" + "\n".join(
                f"${n.strike:.0f} ({n.gex_delta_pct:+.1f}%)" for n in growing[:5]
            )

        desc = "\n".join(lines) + grow_str
        if len(desc) > 3900:
            desc = desc[:3900] + "\n..."

        embed = discord.Embed(
            title=f"📊 0DTE Heatmap | QQQ ${spot:.2f}",
            description=desc,
            color=0x5865F2,
            timestamp=datetime.utcnow(),
        )
        embed.add_field(name="Net GEX", value=f"`{self.gex.net_gex:+,.0f}`", inline=True)
        embed.add_field(name="ATM IV", value=f"`{self.gex.atm_iv:.1%}`", inline=True)
        embed.add_field(name="Refresh #", value=f"{self.gex.update_count}", inline=True)
        embed.set_footer(text=f"Updated {self.gex.last_update:%H:%M:%S}" if self.gex.last_update else "")

        await ctx.send(embed=embed)

    # ── !vwap ────────────────────────────────────────────────────────────
    async def _cmd_vwap(self, ctx):
        """
        Show VWAP deviation strategy overview + backtest performance.
        This is a static info card showing the strategy the bot uses.
        """
        embed = discord.Embed(
            title="📐 VWAP Deviation Strategy — NQ Futures",
            color=0x7B68EE,  # Medium slate blue
            timestamp=datetime.utcnow(),
        )

        embed.description = (
            "**Mean-Reversion at VWAP Standard Deviation Bands**\n"
            "Anchored at Globex open (6 PM ET). Uses GARCH vol regime, "
            "HMM state detection, and microstructure filters (ATR, Kurtosis, "
            "Entropy, Vanna/Vega) to filter entries.\n"
            "\u200b"
        )

        # Strategy mechanics
        embed.add_field(
            name="⚙️ Strategy Logic",
            value=(
                "**Entry:** Price touches ±2SD or ±3SD VWAP band\n"
                "**Direction:** Fade deviation (mean-revert to VWAP)\n"
                "**Exit:** Target at opposite band or VWAP\n"
                "**Stop:** Beyond entry band + buffer (1.5 SD)"
            ),
            inline=False,
        )

        # Filters
        embed.add_field(
            name="🛡️ Active Filters",
            value=(
                "• GARCH vol regime (kills `WEAK_BO`)\n"
                "• High kurtosis + ELEVATED/EXPANSION skip\n"
                "• Death-zone hours (7-9, 12, 15, 16 ET)\n"
                "• Rejection wick filter\n"
                "• Volume spike filter\n"
                "• Momentum exhaustion (ROC)"
            ),
            inline=True,
        )

        # Backtest stats
        embed.add_field(
            name="📊 Backtest (Jun 2022 → Jan 2026)",
            value=(
                "`Trades:       1,339`\n"
                "`Win Rate:     37.3%`\n"
                "`Profit Factor: 1.36`\n"
                "`Sharpe:        1.91`\n"
                "`Sortino:       3.07`\n"
                "`Total PnL:  +3,870 pts`\n"
                "`Max DD:       -$1,763`"
            ),
            inline=True,
        )

        # OOS validation
        embed.add_field(
            name="✅ Out-of-Sample Validation",
            value=(
                "**ROBUST — 5/5 checks pass**\n"
                "OOS (Jan 2025+) **outperforms** IS on all metrics:\n"
                "`Sharpe: 0.81 → 3.92` (+384%)\n"
                "`PF:     1.19 → 1.72` (+45%)\n"
                "`WR:     36.5 → 39.0%` (+7%)\n"
                "`Month Win: 60% → 92%`"
            ),
            inline=False,
        )

        # Key hours
        embed.add_field(
            name="⏰ Best Hours (ET)",
            value=(
                "🥇 **10 AM** — 60% WR, +12.3 avg\n"
                "🥈 **2 PM** — 54% WR, +12.4 avg\n"
                "🥉 **11 AM** — 44% WR, +10.3 avg"
            ),
            inline=True,
        )

        # Regime performance
        embed.add_field(
            name="📈 Best Regimes",
            value=(
                "🏆 **SUPPRESSED** — +7.76/trade\n"
                "🥈 **ELEVATED** — +3.19/trade\n"
                "🥉 **NORMAL** — +1.74/trade"
            ),
            inline=True,
        )

        # Monte Carlo
        embed.add_field(
            name="🎲 Monte Carlo",
            value="**99% profitable** across 200 simulations\n5th pct: $1,844 | 95th pct: $9,258",
            inline=False,
        )

        embed.set_footer(text="VWAP Strategy • Backtested on NQ 5Min data")

        await ctx.send(embed=embed)

    # ── Background Thread Management ────────────────────────────────────

    def start_background(self):
        """Start the Discord bot in a background thread."""
        if not DISCORD_BOT_TOKEN:
            print("[DC-BOT] No DISCORD_BOT_TOKEN set — commands disabled")
            print("[DC-BOT] Create a bot at https://discord.com/developers/applications")
            return False

        def _run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(self.start(DISCORD_BOT_TOKEN))
            except Exception as e:
                print(f"[DC-BOT] Error: {e}")

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        print("[DC-BOT] Starting bot in background...")

        # Wait for bot to be ready (max 20 seconds)
        if self._ready_event.wait(timeout=20):
            print("[DC-BOT] Bot ready!")
        else:
            print("[DC-BOT] WARNING: Bot took too long to connect")

        return True

    def stop_background(self):
        """Stop the Discord bot."""
        if self._loop and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(self.close(), self._loop)
            print("[DC-BOT] Bot stopped")
