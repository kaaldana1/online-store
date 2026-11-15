"""Run: python -m pytest pricing/  (or: python pricing/test_fees.py)"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fees import calc, price_for_net, load_fees  # noqa: E402

F = load_fees()


def approx(a, b, tol=0.011):
    assert abs(a - b) <= tol, (a, b)


def test_ebay_jewelry_40():
    # $40 + $5 ship = $45, taxed 8% = $48.60, 15% = $7.29, + $0.40 order fee = $7.69
    b = calc("ebay", 40, category="jewelry", label_cost=5, fees=F)
    approx(b.fees, 7.69)
    approx(b.net_profit, 40 + 5 - 5 - 7.69)


def test_ebay_small_order_fee():
    b = calc("ebay", 8, label_cost=0, fees=F)
    approx(b.fees, 8 * 1.08 * 0.136 + 0.30)


def test_poshmark_tiers():
    approx(calc("poshmark", 14, fees=F).fees, 2.95)
    approx(calc("poshmark", 15, fees=F).fees, 3.00)
    approx(calc("poshmark", 50, fees=F).fees, 10.00)


def test_facebook():
    # Default is local pickup only (shipping_enabled = false): no fees, no label.
    b = calc("facebook", 40, label_cost=5, fees=F)
    assert b.fees == 0 and b.label_cost == 0
    # Shipped math still works if shipping is ever turned on.
    approx(calc("facebook", 6, local=False, fees=F).fees, 0.40)
    approx(calc("facebook", 40, local=False, fees=F).fees, 2.00)


def test_poshmark_overweight():
    assert calc("poshmark", 50, weight_lb=4, fees=F).label_cost == 0
    assert calc("poshmark", 50, weight_lb=7, fees=F).label_cost == 5
    assert calc("poshmark", 50, weight_lb=12, fees=F).label_cost == 10
    approx(calc("poshmark", 50, weight_lb=7, cost_basis=3, fees=F).net_profit, 50 - 10 - 5 - 3)


def test_price_for_net_roundtrip():
    for p in ("ebay", "facebook", "poshmark"):
        price = price_for_net(p, 25, category="jewelry", label_cost=5, cost_basis=3, fees=F)
        net = calc(p, price, category="jewelry", label_cost=5, cost_basis=3, fees=F).net_profit
        assert net >= 25 and net < 25.05, (p, price, net)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
