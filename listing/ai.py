"""Identify an item from photos and draft listing text with the resale-lister skill.

The skill lives in skills/resale-lister/ (SKILL.md + references/ + scripts/lint_listing.py).
Its instructions are the system prompt (prompt-cached, so repeat items cost ~10% for that part).
Claude may run up to 3 web searches to identify marks/patterns/style numbers (never prices), then
calls the strict `submit_listing` tool. The draft goes through lint_listing.py (plain code); if it
finds errors, they go back to Claude once to fix. Whatever is still wrong is returned in
draft["lint"] so the bot can show it on the intake card.
Every call is logged to `agent_runs` with its token + search cost.
"""
from __future__ import annotations

import base64
import io
import json
import re
import sys
import tomllib
from pathlib import Path

import anthropic
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
CFG = tomllib.loads((ROOT / "listing" / "config.toml").read_text())
FEES = tomllib.loads((ROOT / "platforms" / "fees.toml").read_text())
CATEGORIES = sorted(FEES["ebay"]["categories"].keys())   # same keys the fee calculator uses
SKILL_DIR = ROOT / "skills" / "resale-lister"
sys.path.insert(0, str(SKILL_DIR / "scripts"))
import lint_listing  # noqa: E402

MEDIA_OK = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_REQUESTS = 5          # pause_turn resumes + one lint retry + one "please submit" nudge, at most


def _load_skill() -> tuple[str, dict]:
    """System prompt = SKILL.md (without frontmatter) + every reference file; schema from output-schema.json."""
    body = (SKILL_DIR / "SKILL.md").read_text()
    body = re.sub(r"\A---\n.*?\n---\n", "", body, flags=re.S).strip()
    parts = [body, "\n# Reference files\n\nThe files the workflow tells you to read are included below."]
    for f in sorted((SKILL_DIR / "references").glob("*.md")):
        parts.append(f"\n---\n## references/{f.name}\n\n{f.read_text().strip()}")
    schema = json.loads((SKILL_DIR / "references" / "output-schema.json").read_text())
    # The fee calculator only knows the categories in platforms/fees.toml, so constrain to those.
    schema["properties"]["category"]["enum"] = CATEGORIES
    return "\n".join(parts), schema


SYSTEM, SCHEMA = _load_skill()

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
    """Dollar cost of one response: uncached input, cache writes (1.25x), cache reads (0.1x), output, searches."""
    a = CFG["ai"]
    pin, pout = a["price_in_per_mtok"], a["price_out_per_mtok"]
    cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cr = getattr(usage, "cache_read_input_tokens", 0) or 0
    stu = getattr(usage, "server_tool_use", None)
    searches = (getattr(stu, "web_search_requests", 0) or 0) if stu else 0
    return round((usage.input_tokens * pin + cw * pin * 1.25 + cr * pin * 0.1 + usage.output_tokens * pout) / 1e6
                 + searches * a.get("price_per_web_search", 0.01), 4)


def _tools(web: bool, strict: bool) -> list[dict]:
    tools = []
    if web:
        tools.append({"type": "web_search_20260209", "name": "web_search",
                      "max_uses": CFG["ai"].get("max_web_searches", 3)})
    sub = {"name": "submit_listing",
           "description": "Submit the finished listing draft. Call exactly once per draft (again only if the "
                          "listing check sends back errors).",
           "input_schema": SCHEMA}
    if strict:
        sub["strict"] = True
    tools.append(sub)
    return tools


def _json_from_text(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return None
    return d if isinstance(d, dict) and "ebay_title" in d else None


def _normalize(d: dict) -> dict:
    """Fill anything a non-strict path left out, so the bot never KeyErrors."""
    d = dict(d)
    for k, spec in SCHEMA["properties"].items():
        if k in d:
            continue
        t = spec.get("type")
        d[k] = [] if t == "array" else 0.0 if t == "number" else ""
    if d["category"] not in CATEGORIES:
        d["category"] = "default"
    if d["condition"] not in SCHEMA["properties"]["condition"]["enum"]:
        d["condition"] = "good"
    try:
        d["est_weight_lb"] = float(d["est_weight_lb"]) or 1.0
    except (TypeError, ValueError):
        d["est_weight_lb"] = 1.0
    return d


def identify(photos: list[bytes], notes: str, sku: str | None = None, db=None) -> dict:
    """Return the draft dict described by the skill's output schema, plus:
        lint      list of {"level": "error"|"warn", "field", "message"} still open after the retry
        notices   list of plain-English notes for the seller (e.g. web search unavailable)
        web_searches  how many searches Claude ran
    Raises RuntimeError with a readable message on failure.

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
    content.append({"type": "text", "text":
                    f"Seller's notes: {notes or '(none)'}\n"
                    f"SKU: {sku or '(none)'}\n"
                    f"Fee categories for `category`: {', '.join(CATEGORIES)}\n"
                    "Draft the listing, then call submit_listing."})
    messages: list[dict] = [{"role": "user", "content": content}]

    web = bool(CFG["ai"].get("web_search", True))
    strict = bool(CFG["ai"].get("strict_tool", True))
    notices: list[str] = []
    run = db.execute("INSERT INTO agent_runs (agent, task, model) VALUES ('lister','identify+draft',?)",
                     (CFG["ai"]["model"],)).lastrowid
    db.commit()
    tot = {"in": 0, "out": 0, "usd": 0.0, "searches": 0}

    def call():
        nonlocal web, strict
        while True:
            try:
                return client().beta.messages.create(
                    model=CFG["ai"]["model"],
                    max_tokens=16000,
                    system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
                    tools=_tools(web, strict),
                    tool_choice={"type": "auto"},     # forced tool_choice isn't allowed on this model
                    messages=messages,
                    output_config={"effort": CFG["ai"]["effort"]},
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",              # if the model declines, the API retries on a fallback model
                )
            except anthropic.BadRequestError as e:
                msg = str(e.message).lower()
                if web and ("web_search" in msg or "web search" in msg):
                    web = False
                    notices.append("Web search is off for this API key, so marks and style numbers weren't "
                                   "looked up. Enable web search for your organization in the Claude Console.")
                    continue
                if strict and "strict" in msg:
                    strict = False
                    notices.append("The API rejected strict tool mode; set strict_tool = false in "
                                   "listing/config.toml to skip this retry.")
                    continue
                raise

    def tally(resp):
        u = resp.usage
        tot["in"] += u.input_tokens + (getattr(u, "cache_creation_input_tokens", 0) or 0) \
            + (getattr(u, "cache_read_input_tokens", 0) or 0)
        tot["out"] += u.output_tokens
        tot["usd"] += _cost(u)
        stu = getattr(u, "server_tool_use", None)
        tot["searches"] += (getattr(stu, "web_search_requests", 0) or 0) if stu else 0

    draft, issues, lint_retry_used, nudged = None, [], False, False
    try:
        for _ in range(MAX_REQUESTS):
            resp = call()
            tally(resp)
            if resp.stop_reason in ("refusal", "max_tokens"):
                raise RuntimeError(f"AI couldn't draft this one (stop reason: {resp.stop_reason}).")
            if resp.stop_reason == "pause_turn":      # server-side search loop paused; resend to resume
                messages.append({"role": "assistant", "content": resp.content})
                continue
            use = next((b for b in resp.content if b.type == "tool_use" and b.name == "submit_listing"), None)
            if use is not None:
                cand = use.input if isinstance(use.input, dict) else json.loads(use.input)
            else:
                text = "\n".join(b.text for b in resp.content if b.type == "text")
                cand = _json_from_text(text)
            if cand is None:
                if nudged:
                    break
                nudged = True
                messages += [{"role": "assistant", "content": resp.content},
                             {"role": "user", "content": "Call submit_listing now with the full listing."}]
                continue
            draft = lint_listing.fix(_normalize(cand), sku=sku)
            issues = lint_listing.lint(draft, sku=sku, notes=notes)
            errors = [i for i in issues if i.level == "error"]
            if not errors or lint_retry_used or use is None:
                break
            lint_retry_used = True
            messages += [{"role": "assistant", "content": resp.content},
                         {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use.id,
                                                       "content": lint_listing.for_model(issues),
                                                       "is_error": True}]}]
    except anthropic.APIStatusError as e:
        _fail(db, run, f"API {e.status_code}: {e.message}", tot)
        raise RuntimeError(f"AI request failed ({e.status_code}). Check ANTHROPIC_API_KEY / billing.") from e
    except anthropic.APIConnectionError as e:
        _fail(db, run, "connection error", tot)
        raise RuntimeError("Couldn't reach the AI service (network).") from e
    except RuntimeError as e:
        _fail(db, run, str(e), tot)
        raise

    if draft is None:
        _fail(db, run, "no submit_listing call", tot)
        raise RuntimeError("AI didn't return a listing. Try /newitem again.")

    # Last resort for over-long titles: cut at a word boundary so the packet is always pasteable.
    for key, limit in lint_listing.TITLE_LIMITS.items():
        if len(draft[key]) > limit:
            draft[key] = lint_listing._trim_title(draft[key], limit)
    issues = lint_listing.lint(draft, sku=sku, notes=notes)
    draft["lint"] = [{"level": i.level, "field": i.field, "message": i.message} for i in issues]
    draft["notices"] = notices
    draft["web_searches"] = tot["searches"]
    db.execute("UPDATE agent_runs SET input_tokens=?, output_tokens=?, cost_usd=?, status='ok', "
               "finished_at=datetime('now') WHERE id=?", (tot["in"], tot["out"], round(tot["usd"], 4), run))
    db.commit()
    return draft


def _fail(db, run: int, err: str, tot: dict | None = None) -> None:
    if tot:
        db.execute("UPDATE agent_runs SET input_tokens=?, output_tokens=?, cost_usd=? WHERE id=?",
                   (tot["in"], tot["out"], round(tot["usd"], 4), run))
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
