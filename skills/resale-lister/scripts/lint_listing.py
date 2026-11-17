"""Check a listing draft against the resale-lister rules. No AI, no dependencies.

    python lint_listing.py draft.json [--sku S0042] [--notes "925 stamp"]

As a module:
    draft = fix(draft, sku="S0042")        # safe automatic fixes (tag counts, SKU line, spacing)
    issues = lint(draft, sku="S0042", notes="...")
    errors = [i for i in issues if i.level == "error"]

Errors are things the AI must fix (misdescription, banned claims, missing blocker).
Warnings go to the seller as checks.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass

TITLE_LIMITS = {"ebay_title": 80, "poshmark_title": 80, "facebook_title": 100}
TITLES = tuple(TITLE_LIMITS)
DESC_SOFT, DESC_HARD = 1200, 1500
MAX_STYLE_TAGS, MAX_COLORS, MAX_FB_TAGS, MAX_TAGS, MAX_CHECKS = 3, 2, 10, 10, 5

# Abbreviations that are fine in capitals.
CAPS_OK = {
    "NWT", "NWOT", "NWOB", "NIB", "EUC", "GUC", "VTG", "MCM", "USA", "NYC", "UK", "EPNS",
    "HGE", "RGP", "AWL", "DKNY", "BCBG", "UGG", "XXL", "XXXL", "MOP", "CZ", "OS", "II",
    "III", "IV", "LED", "OEM",
}
HYPE = {r"l@@k": "L@@K", r"\bwow+\b": "WOW", r"must see": "must see", r"\brare\b": "rare",
        r"\bhtf\b": "HTF", r"museum quality": "museum quality", r"!{2,}": "!!", r"\*{2,}": "**",
        r"~{2,}": "~~"}
EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B50\u2B55]")
PLATE = re.compile(r"silver ?plate|silver[- ]plated|\bepns\b|\bepc\b|quadruple plate|"
                   r"triple plate|electroplat|silver on copper|\bplated\b", re.I)
STERLING_EVIDENCE = re.compile(r"sterling|925|lion", re.I)
GOLD_QUALIFIER = re.compile(r"gold[- ]?(filled|plated|tone|toned)|vermeil|\bgf\b|\bhge\b|\brgp\b|"
                            r"gold over|electroplat", re.I)
KARAT = re.compile(r"\b(8|9|10|12|14|18|22|24) ?k(t|arat)?\b|\b(333|375|417|585|750|916)\b", re.I)
NATIVE = re.compile(r"native american|navajo|\bzuni\b|\bhopi\b|indian made|american indian", re.I)
DOCUMENTED = re.compile(r"certificate|documented|documentation|provenance|enrolled|receipt from",
                        re.I)
RESTRICTED = {
    "ivory (if it is the material, not the color)": r"\bivory\b",
    "tortoiseshell": r"tortoise ?shell",
    "rhino horn": r"rhino(ceros)? horn",
    "whalebone / scrimshaw": r"whale ?bone|scrimshaw",
    "protected-bird feathers": r"(eagle|hawk|owl) feather",
}
_BRANDS = (r"tiffany|chanel|coach|louis vuitton|\blv\b|gucci|dior|herm[eè]s|prada|yurman|pandora|"
           r"cartier|kate spade|michael kors|tory burch|\bysl\b|saint laurent|fendi|burberry")
COUNTERFEIT = re.compile(rf"\breplica\b|\bdupe\b|look ?alike|not authentic|"
                         rf"(?:{_BRANDS})[- ]?(?:inspired|style\b(?!\s*(?:no\b|number|#|code|\d))|look\b)|"
                         rf"(?:inspired by|like|similar to|not|faux) (?:{_BRANDS})", re.I)
OFF_PLATFORM = re.compile(r"\bebay\b|poshmark|mercari|depop|etsy|facebook|venmo|paypal|zelle|"
                          r"cash ?app|\btrades?\b|https?://|www\.|\S+@\S+\.\w+|"
                          r"\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}", re.I)
AUTHENTIC = re.compile(r"\bauthentic\b|\bgenuine\b(?! leather)", re.I)
HOME_CLAIMS = re.compile(r"smoke[- ]free|pet[- ]free|non[- ]smoking", re.I)


@dataclass
class Issue:
    level: str      # "error" | "warn"
    field: str
    message: str

    def __str__(self) -> str:
        return f"[{self.level}] {self.field}: {self.message}"


def _all_text(d: dict) -> str:
    specs = " ".join(f"{s.get('name', '')} {s.get('value', '')}" for s in d.get("ebay_item_specifics", []))
    parts = [d.get(t, "") for t in TITLES] + [d.get("description", ""), specs,
             " ".join(d.get("tags", [])), " ".join(d.get("facebook_tags", [])),
             " ".join(d.get("poshmark_style_tags", []))]
    return " ".join(parts)


def _trim_title(t: str, limit: int) -> str:
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) <= limit:
        return t
    cut = t[:limit + 1].rsplit(" ", 1)[0]
    return cut.rstrip(" -–,") if cut else t[:limit]


def fix(d: dict, sku: str | None = None) -> dict:
    """Safe, mechanical fixes. Doesn't change meaning."""
    d = dict(d)
    for key in ("tags", "facebook_tags", "poshmark_style_tags", "poshmark_colors"):
        seen, out = set(), []
        for t in d.get(key, []):
            t = re.sub(r"\s+", " ", str(t).lstrip("#")).strip()
            if t and t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
        d[key] = out
    d["poshmark_style_tags"] = d["poshmark_style_tags"][:MAX_STYLE_TAGS]
    d["poshmark_colors"] = d["poshmark_colors"][:MAX_COLORS]
    d["facebook_tags"] = d["facebook_tags"][:MAX_FB_TAGS]
    d["tags"] = d["tags"][:MAX_TAGS]
    d["checks_before_listing"] = list(d.get("checks_before_listing", []))[:MAX_CHECKS]
    for key in TITLES:
        d[key] = re.sub(r"\s+", " ", d.get(key, "")).strip()
    desc = (d.get("description") or "").strip()
    if sku:
        desc = re.sub(r"\n*SKU:.*$", "", desc, flags=re.I).rstrip()
        desc = f"{desc}\n\nSKU: {sku}"
    d["description"] = desc
    return d


def lint(d: dict, sku: str | None = None, notes: str = "") -> list[Issue]:
    issues: list[Issue] = []
    add = lambda lvl, f, m: issues.append(Issue(lvl, f, m))  # noqa: E731
    evidence = " ".join(d.get("evidence", []))
    material = d.get("material", "")
    blockers = " ".join(d.get("blockers", []))
    everything = _all_text(d)
    plate_known = PLATE.search(f"{material} {evidence} {d.get('description', '')}")

    for key, limit in TITLE_LIMITS.items():
        t = d.get(key, "")
        if not t:
            add("error", key, "missing")
            continue
        if len(t) > limit:
            add("error", key, f"{len(t)} characters; limit is {limit}")
        if EMOJI.search(t):
            add("error", key, "contains emoji")
        for pat in HYPE:
            if re.search(pat, t, re.I):
                add("error", key, f"hype or filler wording ({HYPE[pat]})")
        caps = [w for w in re.findall(r"\b[A-Z]{5,}\b", t) if w not in CAPS_OK]
        if caps:
            add("warn", key, f"ALL CAPS words: {', '.join(caps)}")
        if plate_known and re.search(r"sterling|solid silver", t, re.I) \
                and not re.search(r"sterling handle|hollow handle", t, re.I):
            add("error", key, "says sterling/solid silver but the item is described as plated")
        elif re.search(r"sterling|\b925\b", t, re.I) and not STERLING_EVIDENCE.search(evidence):
            add("warn", key, "claims sterling but `evidence` doesn't mention a sterling/925 mark")
        if re.search(r"\bgold\b", t, re.I) and GOLD_QUALIFIER.search(material) \
                and not GOLD_QUALIFIER.search(t):
            add("error", key, "says gold but the material is filled/plated/tone; say so in the title")
        if re.search(r"solid gold", t, re.I) and not KARAT.search(evidence):
            add("error", key, "says solid gold without a karat stamp in `evidence`")
        if AUTHENTIC.search(t) and d.get("identification_confidence") != "high":
            add("error", key, "uses authentic/genuine without high-confidence identification")
    if len(d.get("ebay_title", "")) < 45:
        add("warn", "ebay_title", "short; use more of the 80 characters for search words")

    desc = d.get("description", "")
    if len(desc) > DESC_HARD:
        add("error", "description", f"{len(desc)} characters; keep under {DESC_SOFT}")
    elif len(desc) > DESC_SOFT:
        add("warn", "description", f"{len(desc)} characters; aim for under {DESC_SOFT}")
    if m := OFF_PLATFORM.search(desc):
        add("error", "description", f"mentions '{m.group(0)}' (other platforms, payment, links or "
                                    "contact details aren't allowed in the shared description)")
    if AUTHENTIC.search(desc) and d.get("identification_confidence") == "low":
        add("warn", "description", "uses authentic/genuine with low identification confidence")
    if HOME_CLAIMS.search(desc) and not HOME_CLAIMS.search(notes):
        add("warn", "description", "claims smoke-free/pet-free, which the seller's notes don't say")
    if sku and not desc.rstrip().endswith(f"SKU: {sku}"):
        add("warn", "description", f"should end with 'SKU: {sku}'")

    if m := NATIVE.search(everything):
        lvl = "warn" if DOCUMENTED.search(notes) else "error"
        add(lvl, "all fields", f"'{m.group(0)}' is a Native American origin claim; without "
                               "documentation use 'Southwestern style' (Indian Arts and Crafts Act)")
    if m := COUNTERFEIT.search(everything):
        add("error", "all fields", f"'{m.group(0)}' reads as a counterfeit or keyword-spam signal")
    for label, pat in RESTRICTED.items():
        if re.search(pat, everything, re.I) and not re.search(pat, blockers, re.I):
            lvl = "warn" if label.startswith("ivory") else "error"
            add(lvl, "blockers", f"mentions {label}; if that's the material, add it to `blockers`")

    if len(d.get("poshmark_style_tags", [])) > MAX_STYLE_TAGS:
        add("warn", "poshmark_style_tags", f"more than {MAX_STYLE_TAGS}")
    if len(d.get("poshmark_colors", [])) > MAX_COLORS:
        add("warn", "poshmark_colors", f"more than {MAX_COLORS}")
    if not d.get("ebay_item_specifics"):
        add("warn", "ebay_item_specifics", "empty; item specifics drive eBay's search filters")
    return issues


def for_model(issues: list[Issue]) -> str:
    errs = [i for i in issues if i.level == "error"]
    return ("The listing check found these problems. Fix exactly these, keep everything else, "
            "and call submit_listing again:\n" + "\n".join(f"- {i.field}: {i.message}" for i in errs))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("draft", help="JSON file with the draft")
    ap.add_argument("--sku")
    ap.add_argument("--notes", default="")
    a = ap.parse_args()
    draft = fix(json.load(open(a.draft)), sku=a.sku)
    found = lint(draft, sku=a.sku, notes=a.notes)
    for i in found:
        print(i)
    print("clean" if not found else f"{len(found)} issue(s)")
    sys.exit(1 if any(i.level == "error" for i in found) else 0)
