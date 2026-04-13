"""
discord_alerts.py - Discord Bot-based Alerts for GEX Bot
=========================================================
Sends rich embed alerts to Discord via the bot when:
  - Signal fires (entry)
  - Trade closes (exit with P&L)
  - GEX heatmap update (periodic)
  - Daily summary

Uses the Discord bot (DISCORD_BOT_TOKEN) instead of webhooks.
"""

import os
import asyncio
import threading
from datetime import datetime
from dotenv import load_dotenv

import discord
from discord.ext import commands

load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID", "")  # Optional: specific channel


class DiscordAlerts:
    def __init__(self):
        self.token = DISCORD_BOT_TOKEN
        self.channel_id = int(DISCORD_CHANNEL_ID) if DISCORD_CHANNEL_ID else None
        self.enabled = bool(self.token)
        self._bot = None
        self._loop = None
        self._thread = None
        self._ready = threading.Event()
        self._channel = None

        if not self.enabled:
            print("[DC] No DISCORD_BOT_TOKEN set — Discord alerts disabled")
            print("[DC] Set DISCORD_BOT_TOKEN in .env to enable")
        else:
            self._start_bot()

    def _start_bot(self):
        """Start the Discord bot in a background thread."""
        intents = discord.Intents.default()
        intents.message_content = True
        self._bot = discord.Client(intents=intents)

        @self._bot.event
        async def on_ready():
            print(f"[DC] Bot connected as {self._bot.user}")
            # Find the channel to send to
            if self.channel_id:
                self._channel = self._bot.get_channel(self.channel_id)
            else:
                # Use first text channel we have access to
                for guild in self._bot.guilds:
                    for ch in guild.text_channels:
                        if ch.permissions_for(guild.me).send_messages:
                            self._channel = ch
                            break
                    if self._channel:
                        break

            if self._channel:
                print(f"[DC] Sending alerts to #{self._channel.name} in {self._channel.guild.name}")
            else:
                print("[DC] WARNING: No text channel found! Check bot permissions.")
                self.enabled = False

            self._ready.set()

        def _run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(self._bot.start(self.token))
            except Exception as e:
                print(f"[DC] Bot error: {e}")
                self.enabled = False
                self._ready.set()

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

        # Wait for bot to be ready (max 15 seconds)
        if not self._ready.wait(timeout=15):
            print("[DC] WARNING: Bot took too long to connect")
            self.enabled = False

    def _send(self, embed_dict, content=None):
        """Send an embed to Discord via the bot."""
        if not self.enabled or not self._channel or not self._loop:
            return False
        try:
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

    def shutdown(self):
        """Cleanly shut down the bot."""
        if self._bot and self._loop and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._bot.close(), self._loop)
            print("[DC] Bot disconnected")

    def signal_alert(self, sig_type, direction, qqq_price, entry_node,
                     target_node, stop_qqq, target_qqq, dex_conf,
                     executed=False, ticket=None, nq_price=None):
        """Send entry signal alert."""

        # Colors: green for LONG, red for SHORT
        color = 0x00FF88 if direction == "LONG" else 0xFF4444

        risk = abs(qqq_price - stop_qqq)
        reward = abs(target_qqq - qqq_price)
        rr = reward / risk if risk > 0 else 0

        target_str = f"${target_node.strike:.0f}" if target_node else f"${target_qqq:.2f}"
        growth_tag = " GROWING" if (target_node and target_node.growing) else ""

        # Confidence bar
        conf_dots = round(dex_conf * 10)
        conf_bar = "🟢" * conf_dots + "⚫" * (10 - conf_dots)

        status = "EXECUTED" if executed else "SIGNAL ONLY"
        status_emoji = "🟢" if executed else "🔵"

        embed = {
            "title": f"{status_emoji} {sig_type} | {direction}",
            "color": color,
            "fields": [
                {
                    "name": "Entry Node",
                    "value": f"**${entry_node.strike:.0f}** ({entry_node.type})\n"
                             f"GEX: `{entry_node.gex:+,.0f}`\n"
                             f"DEX: `{entry_node.dex:+,.0f}` (bias: {entry_node.dex_bias})",
                    "inline": True,
                },
                {
                    "name": "Levels (QQQ)",
                    "value": f"Entry: **${qqq_price:.2f}**\n"
                             f"Stop: ${stop_qqq:.2f}\n"
                             f"Target: {target_str}{growth_tag}",
                    "inline": True,
                },
                {
                    "name": "Risk",
                    "value": f"Risk: ${risk:.2f} | Reward: ${reward:.2f}\n"
                             f"R:R = **1:{rr:.1f}**",
                    "inline": True,
                },
                {
                    "name": "DEX Confidence",
                    "value": f"{dex_conf:.0%} {conf_bar}",
                    "inline": False,
                },
            ],
            "footer": {
                "text": f"{status} | {datetime.now():%H:%M:%S ET}"
                        + (f" | Ticket: {ticket}" if ticket else "")
                        + (f" | NQ: {nq_price:.2f}" if nq_price else ""),
            },
            "timestamp": datetime.utcnow().isoformat(),
        }

        return self._send(embed)

    def trade_closed(self, direction, entry_price, exit_price, pnl,
                     reason, duration_min=0, node_strike=0):
        """Send trade close alert."""
        color = 0x00FF88 if pnl > 0 else 0xFF4444
        emoji = "💰" if pnl > 0 else "💔"

        embed = {
            "title": f"{emoji} Trade Closed | {direction} | {reason}",
            "color": color,
            "fields": [
                {
                    "name": "Result",
                    "value": f"P&L: **${pnl:+,.2f}**\n"
                             f"Entry: ${entry_price:.2f} -> Exit: ${exit_price:.2f}",
                    "inline": True,
                },
                {
                    "name": "Details",
                    "value": f"Node: ${node_strike:.0f}\n"
                             f"Duration: {duration_min:.0f}min\n"
                             f"Exit: {reason}",
                    "inline": True,
                },
            ],
            "timestamp": datetime.utcnow().isoformat(),
        }

        return self._send(embed)

    def heatmap_update(self, spot, nodes, net_gex, atm_iv, growing_nodes=None):
        """Send periodic heatmap snapshot."""
        # Build node table
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
                {
                    "name": "Net GEX",
                    "value": f"`{net_gex:+,.0f}`",
                    "inline": True,
                },
                {
                    "name": "ATM IV",
                    "value": f"`{atm_iv:.1%}`",
                    "inline": True,
                },
            ],
            "footer": {"text": f"Updated {datetime.now():%H:%M:%S ET}"},
            "timestamp": datetime.utcnow().isoformat(),
        }

        return self._send(embed)

    def daily_summary(self, trades_today, total_pnl, wins, losses,
                       best_trade, worst_trade, today_trades=None,
                       node_recap=None):
        """
        Send comprehensive end-of-day report with:
        1. Performance scorecard
        2. Trade-by-trade log with levels & outcomes
        3. GEX level recap — which nodes held, broke, and their outcomes
        """
        if not self.enabled:
            return False

        emoji = "📈" if total_pnl > 0 else "📉"
        color = 0x00FF88 if total_pnl > 0 else 0xFF4444
        wr = wins / trades_today * 100 if trades_today > 0 else 0

        # ── Compute advanced stats ──────────────────────────────────────
        pnls = []
        rr_ratios = []
        signal_stats = {}  # sig_type -> {wins, losses, pnl}
        node_stats = {}    # node_strike -> {wins, losses, pnl, trades}

        if today_trades:
            for t in today_trades:
                pnl = t.get("pnl", 0)
                if t.get("status") != "CLOSED":
                    continue
                pnls.append(pnl)

                # R:R calculation
                entry = t.get("qqq_price", 0)
                stop = t.get("stop_qqq", 0)
                target = t.get("target_qqq", 0)
                risk = abs(entry - stop) if stop else 0
                if risk > 0:
                    actual_rr = pnl / risk if risk > 0 else 0
                    rr_ratios.append(actual_rr)

                # Per-signal stats
                sig = t.get("signal", "UNKNOWN")
                if sig not in signal_stats:
                    signal_stats[sig] = {"wins": 0, "losses": 0, "pnl": 0, "count": 0}
                signal_stats[sig]["count"] += 1
                signal_stats[sig]["pnl"] += pnl
                if pnl > 0:
                    signal_stats[sig]["wins"] += 1
                else:
                    signal_stats[sig]["losses"] += 1

                # Per-node stats
                node_k = f"${t.get('node_strike', 0):.0f}"
                if node_k not in node_stats:
                    node_stats[node_k] = {"wins": 0, "losses": 0, "pnl": 0,
                                          "count": 0, "gex": t.get("node_gex", 0),
                                          "action": t.get("signal", "")[:2]}
                node_stats[node_k]["count"] += 1
                node_stats[node_k]["pnl"] += pnl
                if pnl > 0:
                    node_stats[node_k]["wins"] += 1
                else:
                    node_stats[node_k]["losses"] += 1

        # Profit factor
        gross_wins = sum(p for p in pnls if p > 0)
        gross_losses = abs(sum(p for p in pnls if p < 0))
        pf = gross_wins / gross_losses if gross_losses > 0 else float('inf') if gross_wins > 0 else 0

        # Avg win / avg loss
        win_pnls = [p for p in pnls if p > 0]
        loss_pnls = [p for p in pnls if p < 0]
        avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0
        avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0

        # Avg R:R
        avg_rr = sum(rr_ratios) / len(rr_ratios) if rr_ratios else 0

        # Win/loss streaks
        max_win_streak = max_loss_streak = cur_win = cur_loss = 0
        for p in pnls:
            if p > 0:
                cur_win += 1
                cur_loss = 0
                max_win_streak = max(max_win_streak, cur_win)
            else:
                cur_loss += 1
                cur_win = 0
                max_loss_streak = max(max_loss_streak, cur_loss)

        # Expectancy
        expectancy = total_pnl / trades_today if trades_today > 0 else 0

        # ═══════════════════════════════════════════════════════════════
        #  EMBED 1 — Performance Scorecard
        # ═══════════════════════════════════════════════════════════════

        # Win rate bar
        wr_blocks = round(wr / 10)
        wr_bar = "🟩" * wr_blocks + "🟥" * (10 - wr_blocks)

        pf_display = f"{pf:.2f}" if pf != float('inf') else "∞"

        scorecard = {
            "title": f"{emoji} EOD REPORT — {datetime.now():%A %b %d, %Y}",
            "color": color,
            "fields": [
                {
                    "name": "💰 Net P&L",
                    "value": f"```\n${total_pnl:+,.2f}\n```",
                    "inline": True,
                },
                {
                    "name": "📊 Win Rate",
                    "value": f"**{wr:.0f}%** ({wins}W/{losses}L)\n{wr_bar}",
                    "inline": True,
                },
                {
                    "name": "📈 Profit Factor",
                    "value": f"**{pf_display}**",
                    "inline": True,
                },
                {
                    "name": "💵 Avg Win / Avg Loss",
                    "value": f"Win: **${avg_win:+,.2f}**\nLoss: **${avg_loss:+,.2f}**",
                    "inline": True,
                },
                {
                    "name": "🎯 Expectancy",
                    "value": f"**${expectancy:+,.2f}** / trade",
                    "inline": True,
                },
                {
                    "name": "📏 Avg R:R",
                    "value": f"**{avg_rr:+.2f}R**",
                    "inline": True,
                },
                {
                    "name": "🏆 Best / 💔 Worst",
                    "value": f"Best: **${best_trade:+,.2f}**\nWorst: **${worst_trade:+,.2f}**",
                    "inline": True,
                },
                {
                    "name": "🔥 Streaks",
                    "value": f"Win: **{max_win_streak}** | Loss: **{max_loss_streak}**",
                    "inline": True,
                },
                {
                    "name": "🔢 Total Trades",
                    "value": f"**{trades_today}**",
                    "inline": True,
                },
            ],
            "footer": {"text": f"GEX Bot • {datetime.now():%Y-%m-%d}"},
            "timestamp": datetime.utcnow().isoformat(),
        }

        # Per-signal breakdown
        if signal_stats:
            sig_lines = []
            for sig, st in sorted(signal_stats.items(), key=lambda x: x[1]["pnl"], reverse=True):
                s_wr = st["wins"] / st["count"] * 100 if st["count"] > 0 else 0
                sig_lines.append(
                    f"`{sig:16s}` {st['count']}T | "
                    f"{st['wins']}W/{st['losses']}L ({s_wr:.0f}%) | "
                    f"${st['pnl']:+,.2f}"
                )
            scorecard["fields"].append({
                "name": "📋 Signal Type Breakdown",
                "value": "\n".join(sig_lines),
                "inline": False,
            })

        self._send(scorecard)

        # ═══════════════════════════════════════════════════════════════
        #  EMBED 2 — Trade Log
        # ═══════════════════════════════════════════════════════════════

        if today_trades:
            trade_lines = []
            for i, t in enumerate(today_trades, 1):
                if t.get("status") != "CLOSED":
                    continue
                pnl = t.get("pnl", 0)
                sig = t.get("signal", "?")[:6]
                direction = t.get("direction", "?")
                node = t.get("node_strike", 0)
                entry_px = t.get("qqq_price", 0)
                stop = t.get("stop_qqq", 0)
                target = t.get("target_qqq", 0)

                result_emoji = "✅" if pnl > 0 else "❌"
                exit_reason = t.get("exit_reason", "—")

                risk = abs(entry_px - stop) if stop else 0
                actual_r = pnl / risk if risk > 0 else 0

                trade_lines.append(
                    f"{result_emoji} `#{i}` **{sig}** {direction} @ "
                    f"${node:.0f}\n"
                    f"   Entry ${entry_px:.2f} → ${t.get('exit_price', 0):.2f} | "
                    f"**${pnl:+,.2f}** ({actual_r:+.1f}R) | {exit_reason}"
                )

            if trade_lines:
                # Discord embed description max is 4096 chars, split if needed
                trade_text = "\n".join(trade_lines)
                if len(trade_text) > 3900:
                    trade_text = trade_text[:3900] + "\n..."

                trade_log = {
                    "title": "📝 Trade Log",
                    "color": 0x5865F2,
                    "description": trade_text,
                    "footer": {"text": f"{len(trade_lines)} closed trades"},
                    "timestamp": datetime.utcnow().isoformat(),
                }
                self._send(trade_log)

        # ═══════════════════════════════════════════════════════════════
        #  EMBED 3 — GEX Level Recap
        # ═══════════════════════════════════════════════════════════════

        level_lines = []

        # Traded nodes — performance at each level
        if node_stats:
            level_lines.append("**Traded Levels:**")
            for strike, ns in sorted(node_stats.items(),
                                      key=lambda x: x[1]["pnl"], reverse=True):
                n_wr = ns["wins"] / ns["count"] * 100 if ns["count"] > 0 else 0
                result_emoji = "🟢" if ns["pnl"] > 0 else "🔴"
                gex_sign = "+" if ns.get("gex", 0) > 0 else "-"
                level_lines.append(
                    f"{result_emoji} **{strike}** ({gex_sign}GEX) | "
                    f"{ns['count']} trades ({ns['wins']}W/{ns['losses']}L, "
                    f"{n_wr:.0f}%) | **${ns['pnl']:+,.2f}**"
                )

        # Final snapshot of active nodes (if provided)
        if node_recap:
            level_lines.append("")
            level_lines.append("**End-of-Day GEX Levels:**")
            for n in node_recap[:10]:
                sign = "+" if n.gex > 0 else ""
                growth = " 📈" if n.growing else ""
                shrink = " 📉" if n.shrinking else ""
                # Check if this node was traded
                node_key = f"${n.strike:.0f}"
                traded_tag = ""
                if node_key in node_stats:
                    ns = node_stats[node_key]
                    traded_tag = f" → {ns['wins']}W/{ns['losses']}L"
                level_lines.append(
                    f"`{node_key:>6s}` {n.type[:3]} | "
                    f"GEX:`{sign}{n.gex:>12,.0f}` | "
                    f"{n.action} {n.dex_bias}{growth}{shrink}{traded_tag}"
                )

        if level_lines:
            level_text = "\n".join(level_lines)
            if len(level_text) > 3900:
                level_text = level_text[:3900] + "\n..."

            level_embed = {
                "title": "🗺️ GEX Level Recap",
                "color": 0xFFAA00,
                "description": level_text,
                "footer": {"text": f"EOD snapshot • {datetime.now():%H:%M:%S ET}"},
                "timestamp": datetime.utcnow().isoformat(),
            }
            self._send(level_embed)

        return True

    def bot_status(self, status, message=""):
        """Send bot status update (start/stop/error)."""
        emojis = {"START": "🚀", "STOP": "🛑", "ERROR": "⚠️", "INFO": "ℹ️"}
        colors = {"START": 0x00FF88, "STOP": 0xFF4444, "ERROR": 0xFFAA00, "INFO": 0x5865F2}

        embed = {
            "title": f"{emojis.get(status, 'ℹ️')} Bot {status}",
            "color": colors.get(status, 0x5865F2),
            "description": message or f"GEX Bot {status.lower()} at {datetime.now():%H:%M:%S}",
            "timestamp": datetime.utcnow().isoformat(),
        }

        return self._send(embed)


if __name__ == "__main__":
    # Quick test
    dc = DiscordAlerts()
    if dc.enabled:
        dc.bot_status("INFO", "Discord bot alerts test - connection working!")
        print("[DC] Test message sent!")
    else:
        print("[DC] Set DISCORD_BOT_TOKEN in .env to test")
    dc.shutdown()
