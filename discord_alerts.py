"""
discord_alerts.py - Discord Alerts for VWAP Trading Bot
========================================================
Sends rich embed alerts via the GEXBot's connection.
Does NOT create its own bot — uses the shared bot instance.

Alerts:
  - Signal fires (VWAP entry — MR or BO)
  - Trade closes (exit with P&L)
  - GEX heatmap update (periodic context)
  - Daily summary
  - Bot status (start/stop)
"""

import asyncio
from datetime import datetime


class DiscordAlerts:
    """Send alerts through a shared Discord bot instance."""

    def __init__(self):
        self._bot = None
        self._channel = None
        self._loop = None
        self.enabled = False

    def attach(self, bot, loop):
        """
        Attach to a running GEXBot instance.
        Call this after the bot is ready.
        """
        self._bot = bot
        self._loop = loop
        self.enabled = True

    def set_channel(self, channel):
        """Set the channel to send alerts to."""
        self._channel = channel
        if channel:
            print(f"[DC] Alerts → #{channel.name} in {channel.guild.name}")

    def _send(self, embed_dict, content=None):
        """Send an embed to Discord via the bot."""
        if not self.enabled or not self._channel or not self._loop:
            return False
        try:
            import discord
            embed = discord.Embed.from_dict(embed_dict)

            future = asyncio.run_coroutine_threadsafe(
                self._channel.send(content=content, embed=embed),
                self._loop
            )
            future.result(timeout=10)
            return True
        except Exception as e:
            print(f"[DC] Send error: {e}")
            return False

    def signal_alert(self, direction, mode, entry_zone, nq_price, vwap_level,
                     distance_sd, stop, target, target_label, rr, size_scalar,
                     regime_action, hmm_state, executed=False, ticket=None,
                     reason=""):
        """
        Send VWAP entry signal alert.

        Shows: direction, mode (MR/BO), entry zone (±nσ), VWAP level,
        stop/target, R:R, HMM regime, size scalar.
        """
        color = 0x00FF88 if direction == "LONG" else 0xFF4444
        mode_label = "MEAN REVERSION" if mode == "mean_reversion" else "BREAKOUT"
        mode_emoji = "📐" if mode == "mean_reversion" else "🚀"

        status = "EXECUTED" if executed else "SIGNAL ONLY"
        status_emoji = "🟢" if executed else "🔵"

        # Size bar
        size_dots = round(size_scalar * 10)
        size_bar = "🟩" * size_dots + "⬛" * (10 - size_dots)

        risk = abs(nq_price - stop)
        reward = abs(target - nq_price)

        embed = {
            "title": f"{status_emoji} {mode_emoji} {mode_label} | {direction} | {entry_zone}",
            "color": color,
            "fields": [
                {
                    "name": "📍 Entry Zone",
                    "value": f"**{entry_zone}** at `{nq_price:,.2f}`\n"
                             f"VWAP: `{vwap_level:,.2f}`\n"
                             f"Distance: **{distance_sd:+.2f}σ**",
                    "inline": True,
                },
                {
                    "name": "🎯 Levels",
                    "value": f"Stop: `{stop:,.2f}`\n"
                             f"Target: `{target:,.2f}` ({target_label})\n"
                             f"R:R = **1:{rr:.1f}**",
                    "inline": True,
                },
                {
                    "name": "📊 Risk",
                    "value": f"Risk: `{risk:,.2f}` pts\n"
                             f"Reward: `{reward:,.2f}` pts",
                    "inline": True,
                },
                {
                    "name": "🧠 HMM Regime",
                    "value": f"**{regime_action}** (HMM: {hmm_state})\n"
                             f"Mode: {mode_label}",
                    "inline": True,
                },
                {
                    "name": "📏 Size",
                    "value": f"**{size_scalar:.0%}** {size_bar}",
                    "inline": True,
                },
            ],
            "footer": {
                "text": f"{status} | {datetime.now():%H:%M:%S ET}"
                        + (f" | Ticket: {ticket}" if ticket else ""),
            },
            "timestamp": datetime.utcnow().isoformat(),
        }

        if reason:
            embed["description"] = f"*{reason}*"

        return self._send(embed)

    def trade_closed(self, direction, entry_price, exit_price, pnl,
                     reason, duration_min=0, entry_zone="?", mode="?"):
        """
        Send trade close alert with VWAP context.
        Shows: direction, P&L, entry→exit, duration, entry zone, mode.
        """
        color = 0x00FF88 if pnl > 0 else 0xFF4444
        emoji = "💰" if pnl > 0 else "💔"
        mode_label = "MR" if mode == "mean_reversion" else "BO" if mode == "breakout" else mode.upper()

        embed = {
            "title": f"{emoji} Trade Closed | {direction} | {reason}",
            "color": color,
            "fields": [
                {
                    "name": "💵 Result",
                    "value": f"P&L: **${pnl:+,.2f}**\n"
                             f"Entry: `{entry_price:,.2f}` → Exit: `{exit_price:,.2f}`",
                    "inline": True,
                },
                {
                    "name": "📋 Details",
                    "value": f"Zone: **{entry_zone}** ({mode_label})\n"
                             f"Duration: **{duration_min:.0f}min**\n"
                             f"Exit: {reason}",
                    "inline": True,
                },
            ],
            "timestamp": datetime.utcnow().isoformat(),
        }

        return self._send(embed)

    def heatmap_update(self, spot, nodes, net_gex, atm_iv, growing_nodes=None):
        """Send periodic heatmap snapshot."""
        lines = []
        for n in nodes[:8]:
            sign = "+" if n.gex > 0 else ""
            growth = " ^" if n.growing else ""
            bias_arrow = "⬆" if n.dex_bias == "LONG" else "⬇"
            lines.append(
                f"`${n.strike:>6.0f}` {n.type[:3]} | "
                f"`{sign}{n.gex:>12,.0f}` | "
                f"{n.action} {bias_arrow}{growth}"
            )

        node_table = "\n".join(lines)

        grow_str = ""
        if growing_nodes:
            grow_str = "\n**Growing:** " + ", ".join(
                f"${n.strike:.0f} ({n.gex_delta_pct:+.1f}%)" for n in growing_nodes[:3]
            )

        embed = {
            "title": f"📊 0DTE GEX Heatmap | QQQ ${spot:.2f}",
            "color": 0x5865F2,
            "description": f"{node_table}{grow_str}",
            "fields": [
                {"name": "Net GEX", "value": f"`{net_gex:+,.0f}`", "inline": True},
                {"name": "ATM IV", "value": f"`{atm_iv:.1%}`", "inline": True},
            ],
            "footer": {"text": f"Updated {datetime.now():%H:%M:%S ET}"},
            "timestamp": datetime.utcnow().isoformat(),
        }

        return self._send(embed)

    def daily_summary(self, trades_today, total_pnl, wins, losses,
                       best_trade, worst_trade, today_trades=None,
                       node_recap=None):
        """Send comprehensive end-of-day report."""
        if not self.enabled:
            return False

        emoji = "📈" if total_pnl > 0 else "📉"
        color = 0x00FF88 if total_pnl > 0 else 0xFF4444
        wr = wins / trades_today * 100 if trades_today > 0 else 0

        pnls = []
        rr_ratios = []
        signal_stats = {}

        if today_trades:
            for t in today_trades:
                pnl = t.get("pnl", 0)
                if t.get("status") != "CLOSED":
                    continue
                pnls.append(pnl)

                # Use R:R from trade log
                rr = t.get("rr", 0)
                if rr:
                    rr_ratios.append(rr)

                sig = t.get("signal", "UNKNOWN")
                zone = t.get("entry_zone", "?")
                key = f"{sig} @ {zone}"
                if key not in signal_stats:
                    signal_stats[key] = {"wins": 0, "losses": 0, "pnl": 0, "count": 0}
                signal_stats[key]["count"] += 1
                signal_stats[key]["pnl"] += pnl
                if pnl > 0:
                    signal_stats[key]["wins"] += 1
                else:
                    signal_stats[key]["losses"] += 1

        gross_wins = sum(p for p in pnls if p > 0)
        gross_losses = abs(sum(p for p in pnls if p < 0))
        pf = gross_wins / gross_losses if gross_losses > 0 else float('inf') if gross_wins > 0 else 0

        win_pnls = [p for p in pnls if p > 0]
        loss_pnls = [p for p in pnls if p < 0]
        avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0
        avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0
        avg_rr = sum(rr_ratios) / len(rr_ratios) if rr_ratios else 0

        max_win_streak = max_loss_streak = cur_win = cur_loss = 0
        for p in pnls:
            if p > 0:
                cur_win += 1; cur_loss = 0
                max_win_streak = max(max_win_streak, cur_win)
            else:
                cur_loss += 1; cur_win = 0
                max_loss_streak = max(max_loss_streak, cur_loss)

        expectancy = total_pnl / trades_today if trades_today > 0 else 0
        wr_blocks = round(wr / 10)
        wr_bar = "🟩" * wr_blocks + "🟥" * (10 - wr_blocks)
        pf_display = f"{pf:.2f}" if pf != float('inf') else "∞"

        scorecard = {
            "title": f"{emoji} EOD REPORT — {datetime.now():%A %b %d, %Y}",
            "color": color,
            "fields": [
                {"name": "💰 Net P&L", "value": f"```\n${total_pnl:+,.2f}\n```", "inline": True},
                {"name": "📊 Win Rate", "value": f"**{wr:.0f}%** ({wins}W/{losses}L)\n{wr_bar}", "inline": True},
                {"name": "📈 Profit Factor", "value": f"**{pf_display}**", "inline": True},
                {"name": "💵 Avg Win / Loss", "value": f"Win: **${avg_win:+,.2f}**\nLoss: **${avg_loss:+,.2f}**", "inline": True},
                {"name": "🎯 Expectancy", "value": f"**${expectancy:+,.2f}** / trade", "inline": True},
                {"name": "📏 Avg R:R", "value": f"**{avg_rr:+.2f}R**", "inline": True},
                {"name": "🏆 Best / 💔 Worst", "value": f"Best: **${best_trade:+,.2f}**\nWorst: **${worst_trade:+,.2f}**", "inline": True},
                {"name": "🔥 Streaks", "value": f"Win: **{max_win_streak}** | Loss: **{max_loss_streak}**", "inline": True},
                {"name": "🔢 Total Trades", "value": f"**{trades_today}**", "inline": True},
            ],
            "footer": {"text": f"VWAP Bot • {datetime.now():%Y-%m-%d}"},
            "timestamp": datetime.utcnow().isoformat(),
        }

        if signal_stats:
            sig_lines = []
            for sig, st in sorted(signal_stats.items(), key=lambda x: x[1]["pnl"], reverse=True):
                s_wr = st["wins"] / st["count"] * 100 if st["count"] > 0 else 0
                sig_lines.append(f"`{sig:20s}` {st['count']}T | {st['wins']}W/{st['losses']}L ({s_wr:.0f}%) | ${st['pnl']:+,.2f}")
            scorecard["fields"].append({"name": "📋 Signal Breakdown", "value": "\n".join(sig_lines), "inline": False})

        self._send(scorecard)

        # Trade log
        if today_trades:
            trade_lines = []
            for i, t in enumerate(today_trades, 1):
                if t.get("status") != "CLOSED":
                    continue
                pnl = t.get("pnl", 0)
                zone = t.get("entry_zone", "?")
                mode = t.get("mode", "?")
                mode_short = "MR" if mode == "mean_reversion" else "BO" if mode == "breakout" else mode[:2].upper()
                result_emoji = "✅" if pnl > 0 else "❌"
                trade_lines.append(
                    f"{result_emoji} `#{i}` **{mode_short}** {t.get('direction','?')} @ {zone}\n"
                    f"   {t.get('nq_price', 0):,.2f} → {t.get('exit_price', 0):,.2f} | **${pnl:+,.2f}**"
                )
            if trade_lines:
                trade_text = "\n".join(trade_lines)
                if len(trade_text) > 3900:
                    trade_text = trade_text[:3900] + "\n..."
                self._send({"title": "📝 Trade Log", "color": 0x5865F2, "description": trade_text,
                             "footer": {"text": f"{len(trade_lines)} closed trades"}, "timestamp": datetime.utcnow().isoformat()})

        # Level recap
        level_lines = []
        if node_recap:
            level_lines.append("**End-of-Day GEX Levels:**")
            for n in node_recap[:10]:
                sign = "+" if n.gex > 0 else ""
                level_lines.append(f"`${n.strike:>6.0f}` {n.type[:3]} | GEX:`{sign}{n.gex:>12,.0f}` | {n.action} {n.dex_bias}")

        if level_lines:
            level_text = "\n".join(level_lines)
            if len(level_text) > 3900:
                level_text = level_text[:3900] + "\n..."
            self._send({"title": "🗺️ GEX Level Recap", "color": 0xFFAA00, "description": level_text,
                         "footer": {"text": f"EOD snapshot • {datetime.now():%H:%M:%S ET}"}, "timestamp": datetime.utcnow().isoformat()})

        return True

    def bot_status(self, status, message=""):
        """Send bot status update (start/stop/error)."""
        emojis = {"START": "🚀", "STOP": "🛑", "ERROR": "⚠️", "INFO": "ℹ️"}
        colors = {"START": 0x00FF88, "STOP": 0xFF4444, "ERROR": 0xFFAA00, "INFO": 0x5865F2}

        embed = {
            "title": f"{emojis.get(status, 'ℹ️')} Bot {status}",
            "color": colors.get(status, 0x5865F2),
            "description": message or f"VWAP Bot {status.lower()} at {datetime.now():%H:%M:%S}",
            "timestamp": datetime.utcnow().isoformat(),
        }

        return self._send(embed)

    def shutdown(self):
        """Clean shutdown — disable alerts."""
        self.enabled = False
        self._channel = None
