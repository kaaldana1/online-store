"""Offline tests for the rules-based pricer. Run: python listing/test_pricer.py (or pytest listing/)"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pricer  # noqa: E402


def by(plans):
    return {p.platform: p for p in plans}


def test_uses_the_range():
    p = by(pricer.plan(40, low=30, high=55, category="jewelry", cost=3))
    assert p["ebay"].list_price == 40 and p["facebook"].list_price == 40
    assert p["poshmark"].list_price == 55 and p["poshmark"].floor_price == 40       # floor at the median
    assert p["facebook"].floor_price == 30                                          # floor at the bottom


def test_no_range_falls_back():
    p = by(pricer.plan(40, category="jewelry", cost=3))
    assert p["poshmark"].list_price == 46          # 40 x 1.15
    assert p["facebook"].floor_price == 34         # 40 x 0.85


def test_ranked_by_expected_not_list_profit():
    plans = pricer.plan(40, category="jewelry", cost=3)
    assert [x.expected_net for x in plans] == sorted((x.expected_net for x in plans), reverse=True)
    posh = by(plans)["poshmark"]
    assert posh.expected_net < posh.est_net        # 85% realization


def test_packaging_charged_on_shipped_only():
    p = by(pricer.plan(40, category="jewelry"))
    assert p["facebook"].est_net == 40.0           # local pickup: no fees, no packaging
    import fees
    raw = fees.calc("poshmark", 46, category="jewelry", label_cost=pricer.est_label(1)).net_profit
    assert abs(p["poshmark"].est_net - (raw - 1.0)) < 0.01


def test_melt_floor():
    m = pricer.parse_metal("62g sterling")
    assert (m.metal, m.purity) == ("silver", 0.925)
    assert pricer.parse_metal("4.2 g 14kt").metal == "gold"
    assert pricer.parse_metal("") is None
    floor = pricer.melt_floor(m, {"silver": 30})
    assert abs(floor - 62 / 31.1035 * 0.925 * 30 * 0.80) < 0.01
    for p in pricer.plan(25, metal=m, spot={"silver": 30}, cost=5):
        assert p.list_price >= floor and p.floor_price >= floor
    assert pricer.under_melt(25, m, {"silver": 30})
    assert pricer.melt_floor(m, {}) is None       # no spot price set: no floor, no crash
    try:
        pricer.parse_metal("62g")
    except ValueError:
        pass
    else:
        raise AssertionError("purity is required")


def test_skip_when_expected_profit_too_low():
    p = by(pricer.plan(8, category="jewelry", cost=3))
    assert p["ebay"].skip_reason


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
