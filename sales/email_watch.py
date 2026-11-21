"""Sale email watcher: notice marketplace "you made a sale" emails and tie them to an item.

No AI and nothing touches the marketplaces: it only reads the seller's own inbox over IMAP,
read-only (emails are never marked read, moved or deleted).

    fetch_emails()  IMAP → raw emails from marketplace senders that we haven't seen yet
    parse()         raw email → subject, sender, text (plain + html + decoded links)
    classify()      which platform's sale email is it, if any (rules in email_rules.toml)
    match()         which item sold: SKU in the email → listing link/ID in the email → title

bot/sales_watch.py runs this every few minutes inside the bot and posts the results in #alerts.

Setup (bot/.env):
    EMAIL_ADDRESS=the inbox the marketplaces email when something sells
    EMAIL_APP_PASSWORD=an app password (Gmail: Google Account → Security → App passwords)
    EMAIL_IMAP_HOST=imap.gmail.com      # default
    EMAIL_FOLDER=INBOX                  # default

Try it without changing anything:
    python sales/email_watch.py --dry-run           # check the real inbox, print what it would do
    python sales/email_watch.py --file sale.eml     # test one saved email (Gmail: ⋮ → Download message)
"""
from __future__ import annotations

import argparse
import email
import html
import imaplib
import os
import re
import sqlite3
import sys
import tomllib
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email import policy
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = ROOT / "sales" / "email_rules.toml"


def load_rules(path: Path = RULES_PATH) -> dict:
    rules = tomllib.loads(path.read_text())
    settings = rules.pop("settings", {})
    for pid, r in rules.items():
        r["_sale"] = re.compile(r["sale_subject"])
        r["_not"] = re.compile(r["not_sale_subject"]) if r.get("not_sale_subject") else None
    return {"settings": settings, "platforms": rules}


# ------------------------------------------------------------ parsing
@dataclass
class Email:
    message_id: str
    subject: str
    sender: str
    received_at: str | None
    text: str                                    # plain + html text + every link, URL-decoded
    platform: str | None = None                  # set by classify()


def _html_to_text(s: str) -> str:
    links = " ".join(re.findall(r'href=["\']([^"\']+)["\']', s, flags=re.I))
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return html.unescape(s) + " " + links


def parse(raw: bytes) -> Email:
    msg = email.message_from_bytes(raw, policy=policy.default)
    parts = []
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html") or part.get_content_disposition() == "attachment":
            continue
        try:
            body = part.get_content()
        except (LookupError, UnicodeDecodeError, AssertionError):
            body = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
        parts.append(_html_to_text(body) if ctype == "text/html" else body)
    text = " ".join(parts)
    text = urllib.parse.unquote(urllib.parse.unquote(text))     # tracking links wrap the real URL
    when = None
    try:
        when = parsedate_to_datetime(msg["Date"]).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        pass
    subject = str(msg["Subject"] or "").strip()
    mid = str(msg["Message-ID"] or "").strip() or f"<no-id:{hash((subject, msg['Date']))}>"
    return Email(mid, subject, str(msg["From"] or ""), when, text)


def classify(e: Email, rules: dict) -> Email:
    """Set e.platform to the marketplace whose SALE email this is, or None."""
    sender = e.sender.lower()
    for pid, r in rules["platforms"].items():
        if any(d.lower() in sender for d in r["from"]):
            if r["_sale"].search(e.subject) and not (r["_not"] and r["_not"].search(e.subject)):
                e.platform = pid
            return e
    return e


def is_marketplace(e: Email, rules: dict) -> bool:
    sender = e.sender.lower()
    return any(d.lower() in sender for r in rules["platforms"].values() for d in r["from"])


# ------------------------------------------------------------ matching
# The part of a listing link that identifies it on each site.
LISTING_ID = {
    "ebay": re.compile(r"(?:/itm/(?:[^/?#\s]+/)?|[?&]item=)(\d{9,15})"),
    "poshmark": re.compile(r"/listing/(?:[^/?#\s]*-)?([0-9a-f]{24})\b"),
    "mercari": re.compile(r"/item/(m\d{6,})"),
    "depop": re.compile(r"/products/([a-z0-9][\w-]{3,})", re.I),
    "facebook": re.compile(r"/marketplace/item/(\d{6,})"),
}
SKU_RE = re.compile(r"\b(S\d{4,}|TEST-\d{3,})\b")
PRICE_RE = re.compile(r"(?i)(?:sold for|sale price|item price|price|order total|total|earnings|subtotal)"
                      r"[^$\n]{0,40}\$\s?([\d,]+\.\d{2})")


def listing_key(platform: str, url: str | None) -> str | None:
    if not url or platform not in LISTING_ID:
        return None
    m = LISTING_ID[platform].search(url)
    return m.group(1).lower() if m else None


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s.lower())).strip()


def find_price(e: Email) -> float | None:
    for src in (e.subject, e.text):
        m = PRICE_RE.search(src) or (re.search(r"\$\s?([\d,]+\.\d{2})", src) if src is e.subject else None)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                pass
    return None


@dataclass
class Match:
    how: str | None                       # 'sku' | 'link' | 'title' | None
    item: sqlite3.Row | None = None
    candidates: list[str] = field(default_factory=list)   # SKUs when the title fit more than one item

    @property
    def strong(self) -> bool:
        return self.how in ("sku", "link")


def match(db: sqlite3.Connection, e: Email) -> Match:
    """Which item does this sale email mean? Strongest evidence first."""
    # 1. A SKU in the email (descriptions end with "SKU: S0042"; eBay's Custom label shows in its emails).
    skus = {s.upper() for s in SKU_RE.findall(e.text + " " + e.subject)}
    items = [db.execute("SELECT * FROM items WHERE upper(sku)=?", (s,)).fetchone() for s in sorted(skus)]
    items = [i for i in items if i is not None]
    if len(items) == 1:
        return Match("sku", items[0])

    # 2. A link to (or the ID of) one of our listings on that platform.
    text_l = e.text.lower()
    rows = db.execute("SELECT l.url, l.title, i.* FROM listings l JOIN items i ON i.id=l.item_id "
                      "WHERE l.platform_id=? AND l.status NOT IN ('draft','ended')", (e.platform,)).fetchall()
    hits = {}
    for r in rows:
        key = listing_key(e.platform, r["url"])
        if key and re.search(rf"(?<![0-9a-z]){re.escape(key)}(?![0-9a-z])", text_l):
            hits[r["id"]] = r
    if len(hits) == 1:
        return Match("link", next(iter(hits.values())))

    # 3. The listing title (or its first 40 characters, since emails cut long titles) appears in the email.
    body = " " + _norm(e.subject + " " + e.text) + " "
    titled = {}
    for r in rows:
        t = _norm(r["title"] or "")
        if len(t) >= 12 and f" {t[:40].rsplit(' ', 1)[0] if len(t) > 40 else t} " in body:
            titled[r["id"]] = r
    if len(titled) == 1:
        return Match("title", next(iter(titled.values())))
    cands = sorted({r["sku"] for r in (list(hits.values()) or list(titled.values()))} | {i["sku"] for i in items})
    return Match(None, None, cands)


# ------------------------------------------------------------ IMAP
@dataclass
class ImapConfig:
    address: str
    password: str
    host: str = "imap.gmail.com"
    folder: str = "INBOX"

    @classmethod
    def from_env(cls) -> "ImapConfig | None":
        addr, pw = os.environ.get("EMAIL_ADDRESS", "").strip(), os.environ.get("EMAIL_APP_PASSWORD", "").strip()
        if not addr or not pw:
            return None
        return cls(addr, pw.replace(" ", ""), os.environ.get("EMAIL_IMAP_HOST", "imap.gmail.com").strip(),
                   os.environ.get("EMAIL_FOLDER", "INBOX").strip())


def _check(typ: str, data, what: str):
    if typ != "OK":
        raise RuntimeError(f"IMAP {what} failed: {data!r}")
    return data


def fetch_emails(cfg: ImapConfig, rules: dict, since: datetime, seen: set[str], limit: int = 200) -> list[Email]:
    """Unseen emails from marketplace senders since `since`. Raises on any connection/login problem,
    so the caller can alert. Read-only: BODY.PEEK and a readonly SELECT never change the mailbox."""
    out: list[Email] = []
    day = since.strftime("%d-%b-%Y")
    with imaplib.IMAP4_SSL(cfg.host, 993, timeout=60) as im:
        _check(*im.login(cfg.address, cfg.password), "login")
        _check(*im.select(f'"{cfg.folder}"', readonly=True), f"open folder {cfg.folder}")
        nums: set[bytes] = set()
        for r in rules["platforms"].values():
            for dom in r["from"]:
                data = _check(*im.search(None, "SINCE", day, "FROM", f'"{dom}"'), "search")
                nums.update(data[0].split())
        for n in sorted(nums, key=int)[-limit:]:
            hdr = _check(*im.fetch(n, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])"), "fetch header")
            raw_h = b"".join(p[1] for p in hdr if isinstance(p, tuple))
            mid = str(email.message_from_bytes(raw_h, policy=policy.default)["Message-ID"] or "").strip()
            if mid and mid in seen:
                continue
            data = _check(*im.fetch(n, "(BODY.PEEK[])"), "fetch")
            raw = b"".join(p[1] for p in data if isinstance(p, tuple))
            out.append(classify(parse(raw), rules))
    return out


# ------------------------------------------------------------ CLI
def _describe(db, e: Email) -> str:
    if not e.platform:
        return f"ignore      {e.subject[:70]}"
    m = match(db, e)
    price = find_price(e)
    who = f"{m.item['sku']} (by {m.how})" if m.item else f"NO MATCH {m.candidates or ''}"
    return f"SALE {e.platform:<8} {who:<22} ${price if price is not None else '?'}   {e.subject[:60]}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="check the real inbox, change nothing")
    g.add_argument("--file", type=Path, nargs="+", help="saved .eml file(s) to test")
    ap.add_argument("--days", type=int, default=7)
    a = ap.parse_args()
    sys.path.insert(0, str(ROOT / "db"))
    from init_db import connect
    env = ROOT / "bot" / ".env"          # inside Docker these are already in the environment
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    db, rules = connect(), load_rules()
    if a.file:
        emails = [classify(parse(p.read_bytes()), rules) for p in a.file]
    else:
        cfg = ImapConfig.from_env()
        if cfg is None:
            sys.exit("Set EMAIL_ADDRESS and EMAIL_APP_PASSWORD in bot/.env first.")
        emails = fetch_emails(cfg, rules, datetime.now(timezone.utc) - timedelta(days=a.days), set())
        print(f"{len(emails)} marketplace emails in the last {a.days} days on {cfg.address}:")
    for e in emails:
        print(_describe(db, e))


if __name__ == "__main__":
    main()
