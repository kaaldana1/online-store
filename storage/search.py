"""Find items by description: "which bin is the golden chalice with vine engravings in?"

Two stages:
  1. keyword search (SQLite FTS5, free, instant) over everything we know about each item:
     name, AI description, brand/material/style, listing titles and tags, SKU, notes.
  2. optional "smart" pass: Claude looks at the top candidates and picks the real matches
     (handles synonyms like goblet ≈ chalice, gold-tone ≈ golden). Costs a fraction of a cent.
"""
from __future__ import annotations

import json
import re

STOP = {"the", "a", "an", "in", "is", "which", "what", "where", "bin", "with", "of", "and", "my", "that",
        "it", "its", "find", "for", "on", "to", "one", "some", "item", "thing", "there"}
ACTIVE = "('sourced','appraised','drafted','listed')"


def item_text(db, item_id: int) -> str:
    i = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    parts = [i["sku"], i["name"] or "", i["description"] or "", i["category"] or "", i["condition"] or ""]
    a = db.execute("SELECT keywords FROM appraisals WHERE item_id=? ORDER BY id DESC LIMIT 1", (item_id,)).fetchone()
    if a and a["keywords"]:
        try:
            k = json.loads(a["keywords"])
            parts += [str(k.get(f, "")) for f in ("brand", "material", "style_era", "search_query")]
        except ValueError:
            pass
    for l in db.execute("SELECT title, tags FROM listings WHERE item_id=?", (item_id,)):
        parts += [l["title"] or "", " ".join(json.loads(l["tags"] or "[]"))]
    return " ".join(p for p in parts if p)


def rebuild(db) -> None:
    db.execute("DELETE FROM items_fts")
    for (iid,) in db.execute(f"SELECT id FROM items WHERE status IN {ACTIVE}").fetchall():
        db.execute("INSERT INTO items_fts (item_id, body) VALUES (?, ?)", (iid, item_text(db, iid)))
    db.commit()


def _fts_query(q: str) -> str:
    words = [w for w in re.findall(r"[a-z0-9]+", q.lower()) if len(w) > 1 and w not in STOP]
    return " OR ".join(f'"{w}"*' for w in words)


def find(db, q: str, limit: int = 10) -> list[dict]:
    """Keyword stage. Returns [{id, sku, name, bin_id, photo, score}] best first."""
    rebuild(db)
    fq = _fts_query(q)
    if not fq:
        return []
    rows = db.execute(
        "SELECT i.id, i.sku, i.name, i.bin_id, bm25(items_fts) AS score, "
        "(SELECT path FROM item_photos p WHERE p.item_id=i.id ORDER BY is_primary DESC, id LIMIT 1) AS photo "
        "FROM items_fts JOIN items i ON i.id = items_fts.item_id "
        "WHERE items_fts MATCH ? ORDER BY score LIMIT ?", (fq, limit)).fetchall()
    return [dict(r) for r in rows]


def smart_find(db, q: str, limit: int = 5) -> list[dict]:
    """Keyword stage, then Claude picks which candidates actually match. Falls back to keywords on error."""
    cands = find(db, q, limit=15)
    if len(cands) <= 1:
        return cands
    import ai  # listing/ai.py
    texts = [{"item_id": c["id"], "text": item_text(db, c["id"])[:600]} for c in cands]
    try:
        picks = ai.rank_matches(q, texts)
    except Exception:
        return cands[:limit]
    by_id = {c["id"]: c for c in cands}
    out = []
    for p in picks:
        c = by_id.get(p["item_id"])
        if c:
            out.append({**c, "confidence": p["confidence"], "why": p["reason"]})
    return out[:limit] or cands[:limit]
