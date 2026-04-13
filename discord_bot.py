"""
discord_bot.py - Interactive Discord Bot with Commands
=======================================================
Runs alongside the main trading loop in a background thread.
Responds to commands:
  !levels   - Current + historical GEX levels for the day
  !status   - Bot status, daily P&L, position info
  !heatmap  - Current GEX heatmap snapshot

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
    """Discord bot with GEX commands. Shares state with the trading loop."""

    def __init__(self, gex_engine=None, day_tracker=None, trade_logger=None,
                 signal_engine=None, mt5_live=False):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

        self.gex = gex_engine
        self.tracker = day_tracker
        self.logger = trade_logger
        self.signal_engine = signal_engine
        self.mt5_live = mt5_live
        self._thread = None
        self._loop = None

        # Register commands
        self.add_command(commands.Command(self.levels, name="levels"))
        self.add_command(commands.Command(self.status, name="status"))
        self.add_command(commands.Command(self.heatmap, name="heatmap"))

    async def on_ready(self):
        print(f"[DC-BOT] Logged in as {self.user} (ID: {self.user.id})")

    # ── !levels ─────────────────────────────────────────────────────────
    async def levels(self, ctx):
        """Show all current and historical GEX levels for today."""
        if not self.gex or not self.tracker:
            await ctx.send("⚠️ GEX engine not connected.")
            return

        active, historical = self.tracker.get_all_levels(self.gex.nodes)
        spot = self.gex.spot

        # ── Active Levels ──
        active_lines = []
        for lv in active:
            gex_sign = lv["gex_sign"]
            growth = " 📈" if lv.get("was_growing") else ""
            change = lv["gex_change_pct"]
            change_str = f" ({change:+.0f}%)" if abs(change) > 5 else ""

            # Distance from spot
            dist = lv["strike"] - spot
            dist_str = f"{dist:+.2f}" if dist != 0 else "ATM"

            # Spot marker
            spot_marker = " ◀ SPOT" if abs(dist) < 0.50 else ""

            active_lines.append(
                f"`${lv['strike']:>6.0f}` {lv['type'][:3]} | "
                f"{gex_sign}GEX `{lv['last_gex']:>+13,.0f}`{change_str} | "
                f"{lv['action']} → {lv['dex_bias']}{growth}{spot_marker}"
            )

        # ── Historical Levels ──
        hist_lines = []
        for lv in historical:
            dur = lv["duration_min"]
            first_t = lv["first_seen"].strftime("%H:%M")
            last_t = lv["last_seen"].strftime("%H:%M")
            change = lv["gex_change_pct"]

            hist_lines.append(
                f"`${lv['strike']:>6.0f}` {lv['type'][:3]} | "
                f"Peak: `{lv['max_gex']:>12,.0f}` | "
                f"{lv['action']} | {first_t}→{last_t} ({dur:.0f}m)"
            )

        # Build embed
        desc_parts = []
        if active_lines:
            desc_parts.append(f"**🟢 Active Levels** ({len(active_lines)})\n" +
                              "\n".join(active_lines))
        else:
            desc_parts.append("**🟢 Active Levels**\nNo active nodes")

        if hist_lines:
            desc_parts.append(f"\n**⚪ Historical Levels** ({len(hist_lines)})\n" +
                              "\n".join(hist_lines))

        desc = "\n".join(desc_parts)
        if len(desc) > 3900:
            desc = desc[:3900] + "\n..."

        embed = discord.Embed(
            title=f"🗺️ GEX Levels — {datetime.now():%b %d} | QQQ ${spot:.2f}",
            description=desc,
            color=0x5865F2,
            timestamp=datetime.utcnow(),
        )

        total = len(active) + len(historical)
        embed.set_footer(text=f"{len(active)} active | {len(historical)} expired | "
                              f"{total} total today")

        await ctx.send(embed=embed)

    # ── !status ─────────────────────────────────────────────────────────
    async def status(self, ctx):
        """Show current bot status, daily P&L, and state."""
        mode = "🟢 LIVE" if self.mt5_live else "🔵 SIGNAL-ONLY"
        state = self.signal_engine.state if self.signal_engine else "UNKNOWN"
        spot = self.gex.spot if self.gex else 0

        daily_pnl = self.logger.daily_pnl() if self.logger else 0
        daily_trades = self.logger.daily_trades() if self.logger else 0

        # Node info
        nodes_str = "No nodes loaded"
        if self.gex and self.gex.nodes:
            above = self.gex.get_node_above(spot)
            below = self.gex.get_node_below(spot)
            above_str = f"${above.strike:.0f} ({above.action})" if above else "—"
            below_str = f"${below.strike:.0f} ({below.action})" if below else "—"
            nodes_str = f"Above: {above_str}\nBelow: {below_str}"

        embed = discord.Embed(
            title="📊 Bot Status",
            color=0x00FF88 if daily_pnl >= 0 else 0xFF4444,
            timestamp=datetime.utcnow(),
        )
        embed.add_field(name="Mode", value=mode, inline=True)
        embed.add_field(name="State", value=f"`{state}`", inline=True)
        embed.add_field(name="QQQ", value=f"${spot:.2f}", inline=True)
        embed.add_field(name="Daily P&L", value=f"**${daily_pnl:+,.2f}**", inline=True)
        embed.add_field(name="Trades Today", value=f"{daily_trades}", inline=True)
        embed.add_field(name="Nearest Nodes", value=nodes_str, inline=False)

        if self.gex and self.gex.last_update:
            embed.set_footer(text=f"Last GEX refresh: {self.gex.last_update:%H:%M:%S}")

        await ctx.send(embed=embed)

    # ── !heatmap ────────────────────────────────────────────────────────
    async def heatmap(self, ctx):
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
        print("[DC-BOT] Starting command bot in background...")
        return True

    def stop_background(self):
        """Stop the Discord bot."""
        if self._loop and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(self.close(), self._loop)
            print("[DC-BOT] Bot stopped")
