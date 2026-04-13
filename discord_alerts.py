"""
discord_alerts.py - Discord Alerts for GEX Bot
================================================
Sends rich embed alerts via the GEXBot's connection.
Does NOT create its own bot — uses the shared bot instance.

Alerts:
  - Signal fires (entry)
  - Trade closes (exit with P&L)
  - GEX heatmap update (periodic)
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

    def signal_alert(self, sig_type, direction, qqq_price, entry_node,
                     target_node, stop_qqq, target_qqq, dex_conf,
                     executed=False, ticket=None, nq_price=None):
        """Send entry signal alert."""
        color = 0x00FF88 if direction == "LONG" else 0xFF4444

        risk = abs(qqq_price - stop_qqq)
        reward = abs(target_qqq - qqq_price)
        rr = reward / risk if risk > 0 else 0

        target_str = f"${target_node.strike:.0f}" if target_node else f"${target_qqq:.2f}"
        growth_tag = " GROWING" if (target_node and target_node.growing) else ""

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
        node_stats = {}

        if today_trades:
            for t in today_trades:
                pnl = t.get("pnl", 0)
                if t.get("status") != "CLOSED":
                    continue
                pnls.append(pnl)

                entry = t.get("qqq_price", 0)
                stop = t.get("stop_qqq", 0)
                risk = abs(entry - stop) if stop else 0
                if risk > 0:
                    rr_ratios.append(pnl / risk)

                sig = t.get("signal", "UNKNOWN")
                if sig not in signal_stats:
                    signal_stats[sig] = {"wins": 0, "losses": 0, "pnl": 0, "count": 0}
                signal_stats[sig]["count"] += 1
                signal_stats[sig]["pnl"] += pnl
                if pnl > 0:
                    signal_stats[sig]["wins"] += 1
                else:
                    signal_stats[sig]["losses"] += 1

                node_k = f"${t.get('node_strike', 0):.0f}"
                if node_k not in node_stats:
                    node_stats[node_k] = {"wins": 0, "losses": 0, "pnl": 0,
                                          "count": 0, "gex": t.get("node_gex", 0)}
                node_stats[node_k]["count"] += 1
                node_stats[node_k]["pnl"] += pnl
                if pnl > 0:
                    node_stats[node_k]["wins"] += 1
                else:
                    node_stats[node_k]["losses"] += 1

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
            "footer": {"text": f"GEX Bot • {datetime.now():%Y-%m-%d}"},
            "timestamp": datetime.utcnow().isoformat(),
        }

        if signal_stats:
            sig_lines = []
            for sig, st in sorted(signal_stats.items(), key=lambda x: x[1]["pnl"], reverse=True):
                s_wr = st["wins"] / st["count"] * 100 if st["count"] > 0 else 0
                sig_lines.append(f"`{sig:16s}` {st['count']}T | {st['wins']}W/{st['losses']}L ({s_wr:.0f}%) | ${st['pnl']:+,.2f}")
            scorecard["fields"].append({"name": "📋 Signal Breakdown", "value": "\n".join(sig_lines), "inline": False})

        self._send(scorecard)

        # Trade log
        if today_trades:
            trade_lines = []
            for i, t in enumerate(today_trades, 1):
                if t.get("status") != "CLOSED":
                    continue
                pnl = t.get("pnl", 0)
                node = t.get("node_strike", 0)
                entry_px = t.get("qqq_price", 0)
                stop = t.get("stop_qqq", 0)
                risk = abs(entry_px - stop) if stop else 0
                actual_r = pnl / risk if risk > 0 else 0
                result_emoji = "✅" if pnl > 0 else "❌"
                trade_lines.append(
                    f"{result_emoji} `#{i}` **{t.get('signal','?')[:6]}** {t.get('direction','?')} @ ${node:.0f}\n"
                    f"   ${entry_px:.2f} → ${t.get('exit_price', 0):.2f} | **${pnl:+,.2f}** ({actual_r:+.1f}R)"
                )
            if trade_lines:
                trade_text = "\n".join(trade_lines)
                if len(trade_text) > 3900:
                    trade_text = trade_text[:3900] + "\n..."
                self._send({"title": "📝 Trade Log", "color": 0x5865F2, "description": trade_text,
                             "footer": {"text": f"{len(trade_lines)} closed trades"}, "timestamp": datetime.utcnow().isoformat()})

        # Level recap
        level_lines = []
        if node_stats:
            level_lines.append("**Traded Levels:**")
            for strike, ns in sorted(node_stats.items(), key=lambda x: x[1]["pnl"], reverse=True):
                n_wr = ns["wins"] / ns["count"] * 100 if ns["count"] > 0 else 0
                gex_sign = "+" if ns.get("gex", 0) > 0 else "-"
                level_lines.append(f"{'🟢' if ns['pnl'] > 0 else '🔴'} **{strike}** ({gex_sign}GEX) | {ns['count']}T ({n_wr:.0f}%) | **${ns['pnl']:+,.2f}**")

        if node_recap:
            level_lines.append("\n**End-of-Day GEX Levels:**")
            for n in node_recap[:10]:
                sign = "+" if n.gex > 0 else ""
                node_key = f"${n.strike:.0f}"
                traded_tag = f" → {node_stats[node_key]['wins']}W/{node_stats[node_key]['losses']}L" if node_key in node_stats else ""
                level_lines.append(f"`{node_key:>6s}` {n.type[:3]} | GEX:`{sign}{n.gex:>12,.0f}` | {n.action} {n.dex_bias}{traded_tag}")

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
            "description": message or f"GEX Bot {status.lower()} at {datetime.now():%H:%M:%S}",
            "timestamp": datetime.utcnow().isoformat(),
        }

        return self._send(embed)
