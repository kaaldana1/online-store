"""Platform fee and profit calculator. No AI and no dependencies (Python 3.11+).

Reads platforms/fees.toml. Examples:

    python pricing/fees.py 40 --category jewelry --label 5.50 --cost 3
    python pricing/fees.py --target-net 25 --category jewelry --label 5.50 --cost 3

The first prints each platform's fees and net profit for a $40 item. The second
finds the lowest list price on each platform that still nets $25 profit.
"""
from __future__ import annotations

import argparse
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

FEES_PATH = Path(__file__).resolve().parent.parent / "platforms" / "fees.toml"


def load_fees(path: Path = FEES_PATH) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


@dataclass
class Breakdown:
    platform: str
    list_price: float
    shipping_charged: float   # what the buyer pays the seller for shipping
    label_cost: float         # what the seller pays for the label
    fees: float
    cost_basis: float
    promo_fee: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def net_profit(self) -> float:
        return round(self.list_price + self.shipping_charged - self.label_cost
                     - self.fees - self.promo_fee - self.cost_basis, 2)


def _tiered(amount: float, tier: dict) -> float:
    cap = tier["cap"]
    return min(amount, cap) * tier["rate"] + max(0.0, amount - cap) * tier["rate_above"]


def _tier(p: dict, category: str) -> dict:
    cats = p.get("categories", {})
    return cats.get(category, cats["default"])


def calc(platform: str, list_price: float, *, category: str = "default",
         label_cost: float = 0.0, shipping_charged: float | None = None,
         cost_basis: float = 0.0, local: bool | None = None, weight_lb: float = 0.0,
         promo_rate: float = 0.0,
         fees: dict | None = None) -> Breakdown:
    """Fees and net profit for selling one item on one platform."""
    fees = fees or load_fees()
    p = fees[platform]
    tax_rate = fees["meta"]["default_sales_tax_rate"]
    notes: list[str] = []

    if platform == "ebay":
        # Default: the buyer pays the actual label cost as shipping.
        ship = label_cost if shipping_charged is None else shipping_charged
        order_total = list_price + ship
        taxed_total = order_total * (1 + tax_rate)
        fee = _tiered(taxed_total, _tier(p, category))
        fee += p["per_order_fee_small"] if order_total <= p["per_order_threshold"] else p["per_order_fee_large"]
        promo = taxed_total * promo_rate
        return Breakdown(platform, list_price, ship, label_cost, round(fee, 2), cost_basis,
                         round(promo, 2), notes)

    if platform == "facebook":
        if local is None:                      # default: whatever the fee table says
            local = not p.get("shipping_enabled", True)
        if local:
            return Breakdown(platform, list_price, 0.0, 0.0, 0.0, cost_basis,
                             notes=["local pickup: no fees"])
        ship = label_cost if shipping_charged is None else shipping_charged
        if list_price <= p["shipped_min_threshold"]:
            fee = p["shipped_min_fee"]
        else:
            fee = list_price * p["shipped_rate"]
        return Breakdown(platform, list_price, ship, label_cost, round(fee, 2), cost_basis, notes=notes)

    if platform == "poshmark":
        # The buyer pays Poshmark's prepaid label. Over 5 lb the seller pays a surcharge.
        if list_price < p["flat_fee_threshold"]:
            fee = p["flat_fee_under"]
        else:
            fee = list_price * p["rate"]
        surcharge = 0.0
        if weight_lb > p["label_max_lb"]:
            surcharge = next(t["surcharge"] for t in p["overweight"] if weight_lb <= t["max_lb"])
            notes.append(f"overweight label surcharge ${surcharge:.2f}")
        notes.append(f"buyer pays ${p['label_price_buyer']:.2f} label")
        return Breakdown(platform, list_price, 0.0, surcharge, round(fee, 2), cost_basis, notes=notes)

    raise ValueError(f"no fee model for platform '{platform}' yet")


def enabled_platforms(fees: dict) -> list[str]:
    return [k for k, v in fees.items()
            if isinstance(v, dict) and v.get("enabled") and k != "meta"]


def price_for_net(platform: str, target_net: float, **kw) -> float:
    """Lowest list price (rounded up to the cent) that reaches target_net profit."""
    lo, hi = 0.0, 100_000.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if calc(platform, mid, **kw).net_profit >= target_net:
            hi = mid
        else:
            lo = mid
    return round(hi + 0.005, 2)


def compare(list_price: float, **kw) -> list[Breakdown]:
    fees = kw.pop("fees", None) or load_fees()
    rows = [calc(p, list_price, fees=fees, **kw) for p in enabled_platforms(fees)]
    return sorted(rows, key=lambda b: b.net_profit, reverse=True)


def _main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("price", nargs="?", type=float, help="list price to evaluate")
    ap.add_argument("--target-net", type=float, help="solve for the list price that nets this much")
    ap.add_argument("--category", default="default")
    ap.add_argument("--label", type=float, default=0.0, help="shipping label cost the seller pays")
    ap.add_argument("--cost", type=float, default=0.0, help="what you paid for the item")
    ap.add_argument("--weight", type=float, default=0.0, help="package weight in lb (Poshmark surcharge)")
    a = ap.parse_args()
    fees = load_fees()
    kw = dict(category=a.category, label_cost=a.label, cost_basis=a.cost, weight_lb=a.weight)

    if a.target_net is not None:
        print(f"List price needed to net ${a.target_net:.2f}:")
        for p in enabled_platforms(fees):
            print(f"  {fees[p]['name']:<22} ${price_for_net(p, a.target_net, fees=fees, **kw):>8.2f}")
        return
    if a.price is None:
        ap.error("give a price or --target-net")
    print(f"{'platform':<22}{'fees':>8}{'net':>9}  notes")
    for b in compare(a.price, fees=fees, **kw):
        print(f"{fees[b.platform]['name']:<22}{b.fees:>8.2f}{b.net_profit:>9.2f}  {'; '.join(b.notes)}")


if __name__ == "__main__":
    _main()
