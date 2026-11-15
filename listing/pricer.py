"""Turn a market price into a list price + floor per platform. Rules only, no AI.

    python listing/pricer.py 40 --category jewelry --cost 3 --label 5.50 --weight 1
"""
from __future__ import annotations

import argparse
import math
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pricing"))
import fees as feecalc  # noqa: E402

CFG = tomllib.loads((ROOT / "listing" / "config.toml").read_text())


@dataclass
class Plan:
    platform: str
    list_price: float
    floor_price: float
    est_net: float
    skip_reason: str | None = None


def _round(x: float) -> float:
    return float(math.ceil(x)) if CFG["pricing"]["round_to_dollar"] else round(x, 2)


def est_label(weight_lb: float) -> float:
    """Rough USPS Ground Advantage label cost by packed weight (buyer pays it on eBay,
    but eBay's fee is charged on shipping too, so it still matters)."""
    for row in CFG["pricing"]["label_estimate"]:
        if weight_lb <= row["max_lb"]:
            return row["usd"]
    return CFG["pricing"]["label_estimate"][-1]["usd"]


def plan(market: float, *, category: str = "default", cost: float = 0.0,
         label: float | None = None, weight_lb: float = 1.0) -> list[Plan]:
    fees = feecalc.load_fees()
    p = CFG["pricing"]
    if label is None:
        label = est_label(weight_lb)
    out = []
    for pid in feecalc.enabled_platforms(fees):
        kw = dict(category=category, cost_basis=cost, label_cost=label, weight_lb=weight_lb, fees=fees)
        lp = _round(market * p["multiplier"].get(pid, 1.0))
        net = feecalc.calc(pid, lp, **kw).net_profit
        if net < p["min_profit"]:
            out.append(Plan(pid, lp, lp, net, f"nets only ${net:.2f}"))
            continue
        floor_target = max(p["min_profit"], net * p["floor"]["keep_fraction"])
        floor = min(lp, _round(feecalc.price_for_net(pid, floor_target, **kw)))
        out.append(Plan(pid, lp, floor, net))
    return sorted(out, key=lambda x: (x.skip_reason is not None, -x.est_net))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("market", type=float)
    ap.add_argument("--category", default="default")
    ap.add_argument("--cost", type=float, default=0)
    ap.add_argument("--label", type=float, default=None, help="default: estimated from weight")
    ap.add_argument("--weight", type=float, default=1.0)
    a = ap.parse_args()
    for pl in plan(a.market, category=a.category, cost=a.cost, label=a.label, weight_lb=a.weight):
        tail = f"SKIP ({pl.skip_reason})" if pl.skip_reason else f"floor ${pl.floor_price:.0f}"
        print(f"{pl.platform:<9} list ${pl.list_price:>6.2f}  net ${pl.est_net:>6.2f}  {tail}")
