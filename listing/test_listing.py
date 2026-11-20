"""Offline tests for the resale-lister integration. No API key or network needed: the Claude client is faked.

Run: python listing/test_listing.py   (or: python -m pytest listing/)
"""
import io
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import anthropic
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "listing"))
sys.path.insert(0, str(ROOT / "db"))
import ai  # noqa: E402
import lint_listing  # noqa: E402
from init_db import init  # noqa: E402

GOOD = {
    "name": "Gorham Chantilly sterling dinner fork", "skill_category": "sterling_tableware",
    "category": "default", "brand": "Gorham", "material": "sterling silver (925)", "style_era": "mid century",
    "condition": "good", "condition_notes": "light wear", "est_weight_lb": 0.5,
    "identification_confidence": "high",
    "evidence": ["Back of handle: lion, anchor, 'G' and 'STERLING'."], "checks_before_listing": ["Weigh it in grams."],
    "blockers": [], "sources": [{"url": "https://example.org/chantilly", "confirmed": "pattern name"}],
    "search_query": "Gorham Chantilly sterling dinner fork",
    "ebay_title": "Gorham Chantilly Sterling Silver Dinner Fork No Monogram Vintage",
    "ebay_item_specifics": [{"name": "Brand", "value": "Gorham"}, {"name": "Pattern", "value": "Chantilly"}],
    "poshmark_title": "Sterling silver dinner fork Gorham Chantilly", "poshmark_brand": "Gorham",
    "poshmark_category": "Home > Kitchen > Flatware", "poshmark_size": "OS", "poshmark_colors": ["Silver"],
    "poshmark_style_tags": ["sterling silver", "vintage", "heirloom", "extra"],
    "facebook_title": "Sterling Silver Dinner Fork - Gorham Chantilly", "facebook_tags": ["sterling silver", "#flatware"],
    "description": "Gorham Chantilly sterling silver dinner fork.\nMeasurements: ___", "tags": ["gorham", "sterling"],
}


def photo() -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (40, 40), "white").save(b, format="PNG")
    return b.getvalue()


def usage(searches=0):
    return NS(input_tokens=100, output_tokens=50, cache_creation_input_tokens=0, cache_read_input_tokens=9000,
              server_tool_use=NS(web_search_requests=searches))


def resp(stop, blocks, searches=0):
    return NS(stop_reason=stop, content=blocks, usage=usage(searches))


def submit(d, id_="tu_1"):
    return NS(type="tool_use", name="submit_listing", id=id_, input=d)


class FakeClient:
    def __init__(self, script):
        self.script, self.calls = list(script), []
        self.beta = NS(messages=NS(create=self.create))

    def create(self, **kw):
        self.calls.append(kw)
        nxt = self.script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    init(con)
    return con


def run(script, notes="heavy fork, says sterling"):
    fake = FakeClient(script)
    ai._client = fake
    con = db()
    d = ai.identify([photo()], notes, sku="S0042", db=con)
    return d, fake, con


def test_clean_draft_and_safe_fixes():
    d, fake, con = run([resp("tool_use", [submit(GOOD)], searches=2)])
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["tool_choice"] == {"type": "auto"}
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert [t["name"] for t in call["tools"]] == ["web_search", "submit_listing"]
    assert call["tools"][1]["strict"] is True
    assert call["tools"][1]["input_schema"]["properties"]["category"]["enum"] == ai.CATEGORIES
    assert d["description"].endswith("SKU: S0042")
    assert d["poshmark_style_tags"] == ["sterling silver", "vintage", "heirloom"]
    assert d["facebook_tags"] == ["sterling silver", "flatware"]
    assert not [i for i in d["lint"] if i["level"] == "error"]
    assert d["web_searches"] == 2
    row = con.execute("SELECT status, cost_usd FROM agent_runs").fetchone()
    assert row["status"] == "ok" and row["cost_usd"] > 0.02     # includes the 2 searches


def test_lint_errors_go_back_once():
    bad = dict(GOOD, ebay_title="Rare Sterling Silver Fork L@@K", material="silverplate",
               description="Silver plated fork. Venmo ok.")
    d, fake, _ = run([resp("tool_use", [submit(bad)]), resp("tool_use", [submit(GOOD, "tu_2")])])
    assert len(fake.calls) == 2
    retry = fake.calls[1]["messages"][-1]["content"][0]
    assert retry["type"] == "tool_result" and retry["tool_use_id"] == "tu_1" and retry["is_error"]
    assert "Venmo" in retry["content"] or "venmo" in retry["content"].lower()
    assert d["ebay_title"] == GOOD["ebay_title"]


def test_errors_left_after_retry_are_reported_not_hidden():
    bad = dict(GOOD, description="Fork. Also on Poshmark.")
    d, fake, _ = run([resp("tool_use", [submit(bad)]), resp("tool_use", [submit(bad, "tu_2")])])
    assert len(fake.calls) == 2
    assert any(i["level"] == "error" and i["field"] == "description" for i in d["lint"])


def test_pause_turn_resumes_without_extra_user_message():
    d, fake, _ = run([resp("pause_turn", [NS(type="server_tool_use")]), resp("tool_use", [submit(GOOD)])])
    assert len(fake.calls) == 2
    assert fake.calls[1]["messages"][-1]["role"] == "assistant"


def test_web_search_not_enabled_falls_back():
    import httpx2
    r = httpx2.Response(400, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    err = anthropic.BadRequestError("web_search is not enabled for this organization", response=r, body=None)
    d, fake, _ = run([err, resp("tool_use", [submit(GOOD)])])
    assert [t["name"] for t in fake.calls[1]["tools"]] == ["submit_listing"]
    assert d["notices"] and "Console" in d["notices"][0]


def test_over_long_title_is_trimmed_as_last_resort():
    long = dict(GOOD, ebay_title=GOOD["ebay_title"] + " Heavy Antique Silverware Gift Set")
    d, _, _ = run([resp("tool_use", [submit(long)]), resp("tool_use", [submit(long, "tu_2")])])
    assert len(d["ebay_title"]) <= 80


def test_refusal_is_a_readable_error():
    try:
        run([resp("refusal", [])])
    except RuntimeError as e:
        assert "refusal" in str(e)
    else:
        raise AssertionError("expected RuntimeError")


def test_linter_rules():
    issues = lint_listing.lint(dict(GOOD, ebay_title="Navajo Sterling Silver Cuff Bracelet Turquoise Vintage"))
    assert any(i.level == "error" and "Native American" in i.message for i in issues)
    issues = lint_listing.lint(dict(GOOD, material="gold tone metal", ebay_title="Gold Brooch Pin Vintage Signed Trifari Flower"))
    assert any(i.level == "error" and "gold" in i.message for i in issues)
    issues = lint_listing.lint(dict(GOOD, description="Coach inspired bag"))
    assert any(i.level == "error" and "counterfeit" in i.message for i in issues)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
