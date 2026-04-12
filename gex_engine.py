"""
gex_engine.py - 0DTE GEX/DEX Engine with Node Tracking
========================================================
- Pulls ONLY 0DTE QQQ options chain from Tradier
- Computes per-strike GEX + DEX
- Tracks node size changes over time (growing = stronger magnet)
- Identifies stacked zones and classifies MR vs breakout
"""

import os
import time
import copy
import requests
import numpy as np
from datetime import datetime, timedelta
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()

TRADIER_KEY = os.getenv("TRADIER_API_KEY")
BASE_URL = "https://api.tradier.com/v1"
HEADERS = {"Authorization": f"Bearer {TRADIER_KEY}", "Accept": "application/json"}

STRIKE_RANGE_PCT = 0.08   # +/- 8% of spot
STACK_DISTANCE = 2.0      # $2 spread for stacking detection
MIN_GEX_PCT = 0.05        # Ignore nodes below 5th percentile


class GEXNode:
    """Represents a single GEX node with tracking."""
    def __init__(self, strike, gex, dex, node_type):
        self.strike = strike
        self.gex = gex
        self.dex = dex
        self.type = node_type          # SUPPORT / RESISTANCE
        self.gex_sign = "POS" if gex > 0 else "NEG"
        self.dex_sign = "POS" if dex > 0 else "NEG"

        # Tracking
        self.prev_gex = None           # GEX at last snapshot
        self.gex_delta = 0             # Change since last snapshot
        self.gex_delta_pct = 0         # % change
        self.growing = False           # Is this node getting stronger?
        self.shrinking = False         # Is this node weakening?

        # Stacking
        self.stack_id = None
        self.is_stack_edge = False     # Edge of a stacked zone

    def dex_confidence(self, direction):
        """
        DEX as confidence score (0.0 - 1.0) for a given trade direction.
        High confidence = DEX aligns with the trade thesis.
        
        +GEX (MR): +DEX favors SHORT (dealers selling), -DEX favors LONG
        -GEX (BO): +DEX favors LONG (dealers chasing up), -DEX favors SHORT
        """
        base = 0.5  # Neutral
        dex_norm = min(1.0, abs(self.dex) / max(abs(self.dex), 1)) * 0.5

        if self.gex > 0:
            # +GEX = MR zone. +DEX = dealers selling = SHORT confidence
            if direction == "SHORT" and self.dex > 0:
                return base + dex_norm   # High confidence short
            elif direction == "LONG" and self.dex < 0:
                return base + dex_norm   # High confidence long
            else:
                return base - dex_norm * 0.3  # Lower but still tradeable
        else:
            # -GEX = breakout. +DEX = dealers chasing up = LONG confidence
            if direction == "LONG" and self.dex > 0:
                return base + dex_norm
            elif direction == "SHORT" and self.dex < 0:
                return base + dex_norm
            else:
                return base - dex_norm * 0.3

    @property
    def dex_bias(self):
        """
        Which direction DEX favors (for display only, not for trade decisions).
        """
        if self.gex > 0:
            return "SHORT" if self.dex > 0 else "LONG"
        else:
            return "LONG" if self.dex > 0 else "SHORT"

    @property
    def action(self):
        """MR at +GEX, breakout at -GEX."""
        return "MR" if self.gex > 0 else "BREAKOUT"

    def __repr__(self):
        sign = "+" if self.gex > 0 else ""
        delta_arrow = ""
        if self.growing:
            delta_arrow = " ^GROWING"
        elif self.shrinking:
            delta_arrow = " vSHRINK"
        return (f"${self.strike:.0f} {self.type[:3]} | "
                f"GEX:{sign}{self.gex:,.0f} DEX:{self.dex:+,.0f} | "
                f"{self.action} dex->{self.dex_bias}{delta_arrow}")


class GEXEngine:
    def __init__(self):
        self.spot = 0.0
        self.nodes = []                     # Current GEX nodes (sorted by distance)
        self.strike_gex = {}                # Full per-strike GEX profile
        self.strike_dex = {}                # Full per-strike DEX
        self.prev_strike_gex = {}           # Previous snapshot for delta tracking
        self.net_gex = 0.0
        self.net_dex = 0.0
        self.atm_iv = 0.0
        self.last_update = None
        self.update_count = 0
        self.history = []                   # List of (timestamp, nodes_snapshot)

    def get_spot(self):
        """Get current QQQ price."""
        try:
            r = requests.get(f"{BASE_URL}/markets/quotes",
                params={"symbols": "QQQ"}, headers=HEADERS, timeout=5)
            if r.status_code == 200:
                q = r.json()["quotes"]["quote"]
                self.spot = float(q["last"])
                return self.spot
        except Exception as e:
            print(f"[GEX] Quote error: {e}")
        return self.spot

    def get_0dte_expiration(self):
        """Get today's 0DTE expiration (or next trading day if market closed)."""
        try:
            r = requests.get(f"{BASE_URL}/markets/options/expirations",
                params={"symbol": "QQQ", "includeAllRoots": "true"},
                headers=HEADERS, timeout=5)
            if r.status_code == 200:
                exps = r.json()["expirations"]["date"]
                today = datetime.now().strftime("%Y-%m-%d")
                # Find today's expiration or the nearest future one
                for exp in exps:
                    if exp >= today:
                        return exp
        except Exception as e:
            print(f"[GEX] Expiration error: {e}")
        return None

    def get_chain(self, expiration):
        """Get full options chain for 0DTE expiration."""
        try:
            r = requests.get(f"{BASE_URL}/markets/options/chains",
                params={"symbol": "QQQ", "expiration": expiration, "greeks": "true"},
                headers=HEADERS, timeout=10)
            if r.status_code == 200:
                data = r.json()
                opts = data.get("options", {})
                if opts and "option" in opts:
                    return opts["option"]
        except Exception as e:
            print(f"[GEX] Chain error: {e}")
        return []

    def compute(self):
        """
        Pull 0DTE chain, compute per-strike GEX/DEX, build nodes,
        detect stacking, track growth.
        """
        spot = self.get_spot()
        if spot <= 0:
            return self.nodes

        exp = self.get_0dte_expiration()
        if not exp:
            print("[GEX] No 0DTE expiration found")
            return self.nodes

        chain = self.get_chain(exp)
        if not chain:
            return self.nodes

        lo = spot * (1 - STRIKE_RANGE_PCT)
        hi = spot * (1 + STRIKE_RANGE_PCT)

        # Save previous snapshot for delta tracking
        self.prev_strike_gex = copy.copy(self.strike_gex)

        # Compute per-strike GEX and DEX
        self.strike_gex = defaultdict(float)
        self.strike_dex = defaultdict(float)
        atm_ivs = []

        for opt in chain:
            strike = opt["strike"]
            if strike < lo or strike > hi:
                continue

            oi = opt.get("open_interest", 0) or 0
            greeks = opt.get("greeks") or {}
            gamma = greeks.get("gamma", 0) or 0
            delta = greeks.get("delta", 0) or 0
            iv = greeks.get("mid_iv", 0) or 0

            if opt["option_type"] == "call":
                self.strike_gex[strike] += gamma * oi * 100 * spot
                self.strike_dex[strike] += delta * oi * 100
                if abs(strike - spot) < 2.0 and iv > 0:
                    atm_ivs.append(iv)
            else:
                self.strike_gex[strike] -= gamma * oi * 100 * spot
                self.strike_dex[strike] += delta * oi * 100

        if not self.strike_gex:
            return self.nodes

        # Build nodes from significant strikes
        all_gex = [abs(g) for g in self.strike_gex.values() if g != 0]
        if not all_gex:
            return self.nodes

        threshold = np.percentile(all_gex, MIN_GEX_PCT * 100) if len(all_gex) > 10 else 0

        raw_nodes = []
        for strike in sorted(self.strike_gex.keys()):
            gex = self.strike_gex[strike]
            dex = self.strike_dex.get(strike, 0)

            if abs(gex) < threshold:
                continue

            node_type = "RESISTANCE" if strike >= spot else "SUPPORT"
            node = GEXNode(strike, round(gex, 0), round(dex, 0), node_type)

            # Track GEX delta (growth/shrinkage)
            if self.prev_strike_gex and strike in self.prev_strike_gex:
                prev = self.prev_strike_gex[strike]
                node.prev_gex = prev
                node.gex_delta = gex - prev
                node.gex_delta_pct = (gex - prev) / abs(prev) * 100 if prev != 0 else 0
                # Growing = absolute GEX increasing (node getting stronger)
                node.growing = abs(gex) > abs(prev) * 1.05  # >5% growth
                node.shrinking = abs(gex) < abs(prev) * 0.95  # >5% shrinkage

            raw_nodes.append(node)

        # Detect stacking (nodes within $STACK_DISTANCE of each other)
        raw_nodes.sort(key=lambda n: n.strike)
        stack_id = 0
        for i, node in enumerate(raw_nodes):
            if i == 0:
                node.stack_id = stack_id
            elif abs(node.strike - raw_nodes[i-1].strike) <= STACK_DISTANCE:
                node.stack_id = stack_id
            else:
                stack_id += 1
                node.stack_id = stack_id

        # Mark stack edges
        stacks = defaultdict(list)
        for node in raw_nodes:
            stacks[node.stack_id].append(node)
        for sid, stack_nodes in stacks.items():
            if len(stack_nodes) >= 2:
                stack_nodes[0].is_stack_edge = True
                stack_nodes[-1].is_stack_edge = True

        # Sort by distance from spot
        for node in raw_nodes:
            node.dist = round(abs(node.strike - spot), 2)
        raw_nodes.sort(key=lambda n: n.dist)

        self.nodes = raw_nodes
        self.net_gex = sum(self.strike_gex.values())
        self.net_dex = sum(self.strike_dex.values())
        self.atm_iv = np.mean(atm_ivs) if atm_ivs else 0
        self.last_update = datetime.now()
        self.update_count += 1

        # Save snapshot for history
        self.history.append((
            datetime.now(),
            [(n.strike, n.gex, n.growing) for n in raw_nodes[:10]]
        ))
        # Keep last 60 snapshots (~30 min at 30s refresh)
        if len(self.history) > 60:
            self.history.pop(0)

        return self.nodes

    def get_stacked_zones(self):
        """Get zones of stacked nodes (multiple nodes within $2)."""
        stacks = defaultdict(list)
        for node in self.nodes:
            if node.stack_id is not None:
                stacks[node.stack_id].append(node)

        zones = []
        for sid, stack_nodes in stacks.items():
            if len(stack_nodes) >= 2:
                # Find the 2 biggest nodes in the stack
                by_gex = sorted(stack_nodes, key=lambda n: abs(n.gex), reverse=True)
                zones.append({
                    "id": sid,
                    "nodes": stack_nodes,
                    "strongest": by_gex[0],
                    "second": by_gex[1] if len(by_gex) > 1 else None,
                    "low": min(n.strike for n in stack_nodes),
                    "high": max(n.strike for n in stack_nodes),
                    "avg_gex_sign": "POS" if sum(n.gex for n in stack_nodes) > 0 else "NEG",
                    "width": max(n.strike for n in stack_nodes) - min(n.strike for n in stack_nodes),
                })
        return zones

    def get_growing_nodes(self):
        """Get nodes that are growing in GEX (stronger magnets)."""
        return [n for n in self.nodes if n.growing]

    def get_shrinking_nodes(self):
        """Get nodes that are weakening."""
        return [n for n in self.nodes if n.shrinking]

    def get_nearest_nodes(self, price, n=5):
        """Get N nearest nodes to current price."""
        sorted_by_dist = sorted(self.nodes, key=lambda nd: abs(nd.strike - price))
        return sorted_by_dist[:n]

    def get_node_above(self, price):
        """Get nearest significant node above price."""
        above = [n for n in self.nodes if n.strike > price]
        return min(above, key=lambda n: n.strike) if above else None

    def get_node_below(self, price):
        """Get nearest significant node below price."""
        below = [n for n in self.nodes if n.strike < price]
        return max(below, key=lambda n: n.strike) if below else None

    def print_heatmap(self):
        """Print a text heatmap of the GEX profile around spot."""
        if not self.nodes:
            print("[GEX] No nodes")
            return

        print(f"\n{'='*75}")
        print(f"  0DTE GEX HEATMAP | QQQ ${self.spot:.2f} | {self.last_update:%H:%M:%S} "
              f"| Refresh #{self.update_count}")
        print(f"  Net GEX: {self.net_gex:>+15,.0f} | ATM IV: {self.atm_iv:.1%}")
        print(f"{'='*75}")

        max_gex = max(abs(n.gex) for n in self.nodes) if self.nodes else 1

        # Show nearest 12 nodes
        nearest = self.get_nearest_nodes(self.spot, 12)
        nearest.sort(key=lambda n: n.strike, reverse=True)  # High to low

        for n in nearest:
            bar_len = int(abs(n.gex) / max_gex * 25)
            if n.gex > 0:
                bar = "+" * bar_len
                gex_color = "+"
            else:
                bar = "-" * bar_len
                gex_color = "-"

            # DEX bias indicator (informational only)
            dir_arrow = "^" if n.dex_bias == "LONG" else "v"

            # Growth indicator
            if n.growing:
                growth = " [GROWING]"
            elif n.shrinking:
                growth = " [shrink]"
            else:
                growth = ""

            # Spot marker
            spot_marker = " <<<" if abs(n.strike - self.spot) < 0.50 else ""

            # Stack marker
            stack_mark = f"S{n.stack_id}" if n.is_stack_edge else "  "

            print(f"  ${n.strike:>6.0f} {n.type[:3]} {stack_mark} | "
                  f"{gex_color}GEX:{n.gex:>+13,.0f} DEX:{n.dex:>+10,.0f} | "
                  f"{n.action:>2s} {dir_arrow} | {bar}{spot_marker}{growth}")

        # Show stacked zones
        zones = self.get_stacked_zones()
        if zones:
            print(f"\n  STACKED ZONES:")
            for z in zones:
                print(f"    ${z['low']:.0f}-${z['high']:.0f} ({len(z['nodes'])} nodes, "
                      f"width ${z['width']:.0f}) | "
                      f"Strongest: ${z['strongest'].strike:.0f} "
                      f"({z['strongest'].action} dex->{z['strongest'].dex_bias})")

        # Show growing nodes
        growing = self.get_growing_nodes()
        if growing:
            print(f"\n  GROWING NODES (strengthening magnets):")
            for n in growing[:5]:
                print(f"    ${n.strike:.0f} | GEX delta: {n.gex_delta:+,.0f} "
                      f"({n.gex_delta_pct:+.1f}%)")

        print(f"{'='*75}\n")


if __name__ == "__main__":
    engine = GEXEngine()
    print("Computing 0DTE GEX nodes...", flush=True)
    nodes = engine.compute()
    engine.print_heatmap()

    # Simulate a second refresh to show delta tracking
    print("Refreshing in 5s to show delta tracking...", flush=True)
    time.sleep(5)
    nodes = engine.compute()
    engine.print_heatmap()
