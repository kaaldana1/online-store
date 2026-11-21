"""Turn the comps she found into a list price + lowest offer per platform. Rules only, no AI.

    python listing/pricer.py 40 --low 30 --high 55 --category jewelry --cost 3 --weight 1
    python listing/pricer.py 40 --metal "31g 925" --spot-silver 31.20

What it does (rules in listing/config.toml):
  * uses the sold range: eBay and Facebook list at the typical (median) price, Poshmark near the top
  * lowest offer comes from the range too (Poshmark: the median, Facebook: the bottom)
  * ranks platforms by EXPECTED profit, at the share of list she usually gets (realization rate)
  * subtracts packaging on shipped orders
  * never prices sterling or gold below its melt value x dealer payout, and flags comps under melt
"""
from __future__ import annotations

import argparse
import math
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pricing"))
import fees as feecalc  # noqa: E402

CFG = tomllib.loads((ROOT / "listing" / "config.toml").read_text())
TROY_OZ_G = 31.1035


@dataclass
class Plan:
    platform: str
    list_price: float
    floor_price: float
    est_net: float                    # profit if it sells at list price
    expected_net: float = 0.0         # profit at the price she'll probably get (realization rate)
    skip_reason: str | None = None
    notes: list[str] = field(default_factory=list)


# ------------------------------------------------------------ precious metal
@dataclass
class Metal:
    grams: float
    purity: float
    metal: str                        # 'silver' | 'gold'

    def label(self) -> str:
        return f"{self.grams:g} g {self.metal} ({self.purity * 1000:.0f})"


GOLD_K = {"10k": 0.417, "14k": 0.585, "18k": 0.750, "22k": 0.917, "24k": 0.999}
SILVER = {"999": 0.999, "958": 0.958, "925": 0.925, "sterling": 0.925, "900": 0.900, "coin": 0.900, "800": 0.800}


def parse_metal(text: str | None) -> Metal | None:
    """'31g 925', '31 sterling', '4.2g 14k', '12 g 18kt gold' → Metal. Blank → None. Raises ValueError."""
    if not text or not text.strip():
        return None
    t = text.lower().replace("kt", "k")
    g = re.search(r"(\d+(?:\.\d+)?)\s*(?:g|gr|grams?)\b", t) or re.search(r"(\d+(?:\.\d+)?)", t)
    if not g:
        raise ValueError("no gram weight")
    rest = t[:g.start()] + " " + t[g.end():]
    for k, v in GOLD_K.items():
        if re.search(rf"\b{k}\b", rest):
            return Metal(float(g.group(1)), v, "gold")
    for k, v in SILVER.items():
        if re.search(rf"\b{k}\b", rest):
            return Metal(float(g.group(1)), v, "silver")
    raise ValueError("no purity (use 925, sterling, 800, 14k, 18k...)")


def melt_floor(m: Metal | None, spot: dict | None) -> float | None:
    """What a refiner would pay: grams / 31.1035 x purity x spot x dealer payout."""
    if m is None or not spot or not spot.get(m.metal):
        return None
    return round(m.grams / TROY_OZ_G * m.purity * spot[m.metal] * CFG["pricing"]["melt"]["dealer_payout"], 2)


# ------------------------------------------------------------ pricing
def _round(x: float) -> float:
    return float(math.ceil(x)) if CFG["pricing"]["round_to_dollar"] else round(x, 2)


def est_label(weight_lb: float) -> float:
    """Rough USPS Ground Advantage label cost by packed weight (buyer pays it on eBay,
    but eBay's fee is charged on shipping too, so it still matters)."""
    for row in CFG["pricing"]["label_estimate"]:
        if weight_lb <= row["max_lb"]:
            return row["usd"]
    return CFG["pricing"]["label_estimate"][-1]["usd"]


def _point(where: str, pid: str, market: float, low: float | None, high: float | None) -> float:
    m = CFG["pricing"]["multiplier"]
    if where == "high":
        return high if high is not None else market * m.get(pid, 1.0)
    if where == "low":
        return low if low is not None else market * m.get("low_fallback", 0.85)
    return market


def plan(market: float, *, low: float | None = None, high: float | None = None, category: str = "default",
         cost: float = 0.0, label: float | None = None, weight_lb: float = 1.0,
         metal: Metal | None = None, spot: dict | None = None) -> list[Plan]:
    fees = feecalc.load_fees()
    p = CFG["pricing"]
    if label is None:
        label = est_label(weight_lb)
    if low is not None and high is not None and low > high:
        low, high = high, low
    melt = melt_floor(metal, spot)
    out = []
    for pid in feecalc.enabled_platforms(fees):
        kw = dict(category=category, cost_basis=cost, label_cost=label, weight_lb=weight_lb, fees=fees)
        notes: list[str] = []

        def net(price: float) -> float:
            b = feecalc.calc(pid, price, **kw)
            local = any("local pickup" in n for n in b.notes)
            return round(b.net_profit - (0.0 if local else p["packaging"]["usd"]), 2)

        lp = _round(_point(p["list_at"].get(pid, "median"), pid, market, low, high))
        if melt is not None and lp < melt:
            lp = _round(melt)
            notes.append(f"raised to melt floor ${melt:.0f}")
        rate = p["realization"].get(pid, 1.0)
        at_list, expected = net(lp), net(lp * rate)
        if expected < p["min_profit"]:
            out.append(Plan(pid, lp, lp, at_list, expected,
                            f"nets only ${expected:.2f} at the usual {rate:.0%} of list", notes))
            continue
        min_ok = feecalc.price_for_net(pid, p["min_profit"] + (0 if pid == "facebook" else p["packaging"]["usd"]), **kw)
        where = p["floor_at"].get(pid, "keep")
        if where == "keep":
            target = max(p["min_profit"], at_list * p["floor"]["keep_fraction"])
            floor = feecalc.price_for_net(pid, target + (0 if pid == "facebook" else p["packaging"]["usd"]), **kw)
        else:
            floor = _point(where, pid, market, low, high)
        floor = max(floor, min_ok, melt or 0.0)
        out.append(Plan(pid, lp, min(lp, _round(floor)), at_list, expected, None, notes))
    return sorted(out, key=lambda x: (x.skip_reason is not None, -x.expected_net))


def under_melt(market: float, metal: Metal | None, spot: dict | None) -> float | None:
    """The melt floor when typical comps sell below it (then: sell as a lot or to a refiner)."""
    m = melt_floor(metal, spot)
    return m if m is not None and market < m else None


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("market", type=float)
    ap.add_argument("--low", type=float)
    ap.add_argument("--high", type=float)
    ap.add_argument("--category", default="default")
    ap.add_argument("--cost", type=float, default=0)
    ap.add_argument("--label", type=float, default=None, help="default: estimated from weight")
    ap.add_argument("--weight", type=float, default=1.0)
    ap.add_argument("--metal", help='e.g. "31g 925" or "4.2g 14k"')
    ap.add_argument("--spot-silver", type=float)
    ap.add_argument("--spot-gold", type=float)
    a = ap.parse_args()
    metal = parse_metal(a.metal)
    spot = {"silver": a.spot_silver, "gold": a.spot_gold}
    if (m := melt_floor(metal, spot)) is not None:
        print(f"melt floor ${m:.2f}" + ("  (comps are UNDER melt: sell as a lot or to a refiner)"
                                         if under_melt(a.market, metal, spot) else ""))
    for pl in plan(a.market, low=a.low, high=a.high, category=a.category, cost=a.cost, label=a.label,
                   weight_lb=a.weight, metal=metal, spot=spot):
        tail = f"SKIP ({pl.skip_reason})" if pl.skip_reason else f"floor ${pl.floor_price:.0f}"
        print(f"{pl.platform:<9} list ${pl.list_price:>6.2f}  expected net ${pl.expected_net:>6.2f}  "
              f"(at list ${pl.est_net:>6.2f})  {tail} {' '.join(pl.notes)}")
