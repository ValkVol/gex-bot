"""
discord_alerts.py - Discord Webhook Alerts for GEX Bot
=======================================================
Sends rich embed alerts to Discord when:
  - Signal fires (entry)
  - Trade closes (exit with P&L)
  - GEX heatmap update (periodic)
  - Daily summary

Uses webhooks (no bot token needed) for simplicity.
Can also run as a full bot with interactive commands.
"""

import os
import json
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")


class DiscordAlerts:
    def __init__(self, webhook_url=None):
        self.webhook_url = webhook_url or WEBHOOK_URL
        self.enabled = bool(self.webhook_url)
        if not self.enabled:
            print("[DC] No webhook URL set — Discord alerts disabled")
            print("[DC] Set DISCORD_WEBHOOK_URL in .env to enable")

    def _send(self, embed, content=None):
        """Send an embed to Discord via webhook."""
        if not self.enabled:
            return False
        try:
            payload = {"embeds": [embed]}
            if content:
                payload["content"] = content
            r = requests.post(self.webhook_url, json=payload, timeout=5)
            return r.status_code in (200, 204)
        except Exception as e:
            print(f"[DC] Send error: {e}")
            return False

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

    def daily_summary(self, trades_today, total_pnl, wins, losses, best_trade, worst_trade):
        """Send end-of-day summary."""
        emoji = "📈" if total_pnl > 0 else "📉"
        color = 0x00FF88 if total_pnl > 0 else 0xFF4444
        wr = wins / trades_today * 100 if trades_today > 0 else 0

        embed = {
            "title": f"{emoji} Daily Summary",
            "color": color,
            "fields": [
                {
                    "name": "P&L",
                    "value": f"**${total_pnl:+,.2f}**",
                    "inline": True,
                },
                {
                    "name": "Trades",
                    "value": f"{trades_today} ({wins}W / {losses}L)\nWR: {wr:.0f}%",
                    "inline": True,
                },
                {
                    "name": "Best / Worst",
                    "value": f"Best: ${best_trade:+,.2f}\nWorst: ${worst_trade:+,.2f}",
                    "inline": True,
                },
            ],
            "footer": {"text": f"{datetime.now():%Y-%m-%d}"},
            "timestamp": datetime.utcnow().isoformat(),
        }

        return self._send(embed)

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
        dc.bot_status("INFO", "Discord alerts test - connection working!")
        print("[DC] Test message sent!")
    else:
        print("[DC] Set DISCORD_WEBHOOK_URL in .env to test")
        print("[DC] Create a webhook in Discord: Server Settings > Integrations > Webhooks")
