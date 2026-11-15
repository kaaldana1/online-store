"""Identify an item from photos and draft listing text, using one Claude call.

The call returns strict JSON (structured outputs), so there's nothing to parse loosely.
Every call is logged to `agent_runs` with its token cost.
"""
from __future__ import annotations

import base64
import io
import json
import sys
import tomllib
from pathlib import Path

import anthropic
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
CFG = tomllib.loads((ROOT / "listing" / "config.toml").read_text())
FEES = tomllib.loads((ROOT / "platforms" / "fees.toml").read_text())
CATEGORIES = sorted(FEES["ebay"]["categories"].keys())   # same keys the fee calculator uses

SYSTEM = """You help a thrift-store reseller turn item photos into marketplace listings.
She sells mostly sterling silver, jewelry, and portfolio folders, plus general thrift finds,
on eBay, Poshmark, and Facebook Marketplace.

Identify the item as precisely as the photos allow: maker or brand, material (look for
hallmarks such as 925 or STERLING), style or era, and visible condition issues. Never claim a
brand, material, or authenticity you can't see evidence for. If it's uncertain, say "appears to be"
and add an entry to `checks_before_listing` (e.g. "photograph the clasp hallmark").

Write titles the way buyers search: most important keywords first, no filler words, no
ALL CAPS, no emojis. eBay titles must be 80 characters or fewer, Poshmark 80, Facebook 100.
Descriptions are plain text, honest about flaws, with a measurements line to fill in
(write "Measurements: ___") unless the photos show a ruler. Tags are single words or short
phrases buyers would type, no '#'.
"""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "category", "brand", "material", "style_era", "condition",
                 "condition_notes", "est_weight_lb", "search_query", "ebay_title",
                 "poshmark_title", "facebook_title", "description", "tags",
                 "checks_before_listing"],
    "properties": {
        "name": {"type": "string", "description": "short internal name, e.g. 'sterling floral cuff bracelet'"},
        "category": {"type": "string", "enum": CATEGORIES,
                     "description": "fee category; 'default' unless one of the others clearly applies"},
        "brand": {"type": "string", "description": "maker/brand, or 'unbranded'"},
        "material": {"type": "string"},
        "style_era": {"type": "string"},
        "condition": {"type": "string", "enum": ["new with tags", "like new", "good", "fair", "for parts"]},
        "condition_notes": {"type": "string"},
        "est_weight_lb": {"type": "number", "description": "estimated packed shipping weight in pounds"},
        "search_query": {"type": "string", "description": "4-7 word query to find sold comps on eBay"},
        "ebay_title": {"type": "string"},
        "poshmark_title": {"type": "string"},
        "facebook_title": {"type": "string"},
        "description": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "checks_before_listing": {"type": "array", "items": {"type": "string"}},
    },
}

TITLE_LIMITS = {"ebay_title": 80, "poshmark_title": 80, "facebook_title": 100}
MEDIA_OK = {"image/jpeg", "image/png", "image/gif", "image/webp"}

_client: anthropic.Anthropic | None = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()          # reads ANTHROPIC_API_KEY
    return _client


def prepare_image(data: bytes) -> tuple[str, str]:
    """Shrink a phone photo and return (media_type, base64)."""
    img = Image.open(io.BytesIO(data))
    img = img.convert("RGB")
    img.thumbnail((CFG["ai"]["max_image_px"],) * 2)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85)
    return "image/jpeg", base64.standard_b64encode(out.getvalue()).decode()


def _cost(usage) -> float:
    return round(usage.input_tokens / 1e6 * CFG["ai"]["price_in_per_mtok"]
                 + usage.output_tokens / 1e6 * CFG["ai"]["price_out_per_mtok"], 4)


def identify(photos: list[bytes], notes: str, db=None) -> dict:
    """Return the draft dict described by SCHEMA. Raises RuntimeError with a readable message on failure.

    Runs in a worker thread (the bot calls it via asyncio.to_thread), so by default it opens its
    own SQLite connection for the spend log. SQLite connections can't be shared across threads.
    """
    if db is None:
        sys.path.insert(0, str(ROOT / "db"))
        from init_db import connect
        db = connect()
    content = []
    for p in photos[:4]:
        media, b64 = prepare_image(p)
        content.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}})
    content.append({"type": "text", "text": f"Seller's notes: {notes or '(none)'}\nDraft the listing."})

    run = db.execute("INSERT INTO agent_runs (agent, task, model) VALUES ('lister','identify+draft',?)",
                     (CFG["ai"]["model"],)).lastrowid
    db.commit()
    try:
        resp = client().beta.messages.create(
            model=CFG["ai"]["model"],
            max_tokens=4000,
            system=SYSTEM,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": CFG["ai"]["effort"],
                           "format": {"type": "json_schema", "schema": SCHEMA}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",              # if the model declines, the API retries on a fallback model
        )
    except anthropic.APIStatusError as e:
        _fail(db, run, f"API {e.status_code}: {e.message}")
        raise RuntimeError(f"AI request failed ({e.status_code}). Check ANTHROPIC_API_KEY / billing.") from e
    except anthropic.APIConnectionError as e:
        _fail(db, run, "connection error")
        raise RuntimeError("Couldn't reach the AI service (network).") from e

    db.execute("UPDATE agent_runs SET input_tokens=?, output_tokens=?, cost_usd=? WHERE id=?",
               (resp.usage.input_tokens, resp.usage.output_tokens, _cost(resp.usage), run))
    if resp.stop_reason in ("refusal", "max_tokens"):
        _fail(db, run, f"stop_reason={resp.stop_reason}")
        raise RuntimeError(f"AI couldn't draft this one (stop reason: {resp.stop_reason}).")

    text = next(b.text for b in resp.content if b.type == "text")
    draft = json.loads(text)
    for key, limit in TITLE_LIMITS.items():
        draft[key] = draft[key][:limit].rstrip()
    draft["tags"] = [t.lstrip("#").strip() for t in draft["tags"]][:10]
    db.execute("UPDATE agent_runs SET status='ok', finished_at=datetime('now') WHERE id=?", (run,))
    db.commit()
    return draft


def _fail(db, run: int, err: str) -> None:
    db.execute("UPDATE agent_runs SET status='failed', error=?, finished_at=datetime('now') WHERE id=?", (err, run))
    db.commit()


# ------------------------------------------------------------ step 3: finder re-ranking
RANK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["matches"],
    "properties": {"matches": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["item_id", "confidence", "reason"],
        "properties": {
            "item_id": {"type": "integer"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "reason": {"type": "string", "description": "a few words, e.g. 'gold-tone goblet, vine pattern'"},
        }}}},
}


def rank_matches(query: str, candidates: list[dict], db=None) -> list[dict]:
    """Given the seller's description and candidate items, return the ones that match, best first."""
    if db is None:
        sys.path.insert(0, str(ROOT / "db"))
        from init_db import connect
        db = connect()
    listing = "\n".join(f"[{c['item_id']}] {c['text']}" for c in candidates)
    run = db.execute("INSERT INTO agent_runs (agent, task, model) VALUES ('finder','rank',?)",
                     (CFG["ai"]["model"],)).lastrowid
    db.commit()
    try:
        resp = client().messages.create(
            model=CFG["ai"]["model"],
            max_tokens=2000,
            system="You match a reseller's description of an item to her inventory records. Return only items "
                   "that plausibly match, best first. Treat synonyms and near-descriptions as matches "
                   "(goblet/chalice, gold-tone/golden). Return an empty list if nothing fits.",
            messages=[{"role": "user", "content": f"Looking for: {query}\n\nInventory candidates:\n{listing}"}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": RANK_SCHEMA}},
        )
    except anthropic.APIError as e:
        _fail(db, run, str(e)[:200])
        raise RuntimeError("AI search unavailable") from e
    db.execute("UPDATE agent_runs SET input_tokens=?, output_tokens=?, cost_usd=?, status='ok', "
               "finished_at=datetime('now') WHERE id=?",
               (resp.usage.input_tokens, resp.usage.output_tokens, _cost(resp.usage), run))
    db.commit()
    if resp.stop_reason != "end_turn":
        raise RuntimeError(f"AI search stopped: {resp.stop_reason}")
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)["matches"]
