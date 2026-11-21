"""Offline tests for sale recording, undo, and the sale email watcher. No inbox, Discord or network.

Run: python sales/test_sales.py   (or: python -m pytest sales/)
"""
import asyncio
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sales"))
sys.path.insert(0, str(ROOT / "db"))
sys.path.insert(0, str(ROOT / "bot"))
import email_watch as ew  # noqa: E402
import sale_record as sr  # noqa: E402
from init_db import init  # noqa: E402

RULES = ew.load_rules()
PM_ID = "6543210fedcba9876543210f"


def fresh_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    init(db)
    return db


def add_item(db, sku="S0042", title="Gorham Chantilly Sterling Silver Dinner Fork No Monogram", statuses=None,
             pm_id=PM_ID, ebay_id="123456789012"):
    statuses = statuses or {"ebay": "live", "poshmark": "live", "facebook": "live"}
    iid = db.execute("INSERT INTO items (sku, name, status, bin_id) VALUES (?,?,'listed',NULL)",
                     (sku, "chantilly fork")).lastrowid
    urls = {"ebay": f"https://www.ebay.com/itm/Gorham-fork/{ebay_id}",
            "poshmark": f"https://poshmark.com/listing/Gorham-Chantilly-Fork-{pm_id}",
            "facebook": None}
    for pid, st in statuses.items():
        db.execute("INSERT INTO listings (item_id, platform_id, status, title, list_price, url) VALUES (?,?,?,?,?,?)",
                   (iid, pid, st, title, 45, urls[pid]))
    db.commit()
    return iid


def mail(subject, body="", sender="Poshmark <notifications@poshmark.com>", html=None, mid="<a1@x>"):
    m = EmailMessage()
    m["Subject"], m["From"], m["Message-ID"] = subject, sender, mid
    m["Date"] = "Sat, 21 Nov 2026 10:00:00 -0700"
    m.set_content(body)
    if html:
        m.add_alternative(html, subtype="html")
    return m.as_bytes()


def tasks(db, item_id):
    return {r["platform_id"]: r["status"] for r in db.execute(
        "SELECT l.platform_id, d.status FROM delist_tasks d JOIN listings l ON l.id=d.listing_id WHERE l.item_id=?",
        (item_id,))}


# ------------------------------------------------------------ sale + trigger
def test_sale_creates_manual_tasks_only_for_listings_that_may_be_up():
    db = fresh_db()
    iid = add_item(db, statuses={"ebay": "live", "poshmark": "approved", "facebook": "pending_approval"})
    db.execute("INSERT INTO approvals (kind, ref_table, ref_id, summary) VALUES ('post_listing','items',?,'x')", (iid,))
    res = sr.record_sale(db, iid, "ebay", 44.0)
    assert tasks(db, iid) == {"poshmark": "manual_required"}, tasks(db, iid)   # eBay never waits on a worker
    assert res.to_remove == ["poshmark"]
    st = {r["platform_id"]: r["status"] for r in db.execute("SELECT * FROM listings WHERE item_id=?", (iid,))}
    assert st == {"ebay": "sold", "poshmark": "approved", "facebook": "ended"}, st
    assert db.execute("SELECT status FROM approvals").fetchone()[0] == "expired"
    assert db.execute("SELECT status FROM items WHERE id=?", (iid,)).fetchone()[0] == "sold"


def test_double_sale_is_refused():
    db = fresh_db()
    iid = add_item(db)
    sr.record_sale(db, iid, "facebook", 40)
    try:
        sr.record_sale(db, iid, "ebay", 45)
    except sr.AlreadySold as e:
        assert "Facebook" in str(e)
    else:
        raise AssertionError("second sale should be refused")


def test_unsold_puts_it_back():
    db = fresh_db()
    iid = add_item(db)
    sale = sr.record_sale(db, iid, "poshmark", 48)
    t = db.execute("SELECT d.id FROM delist_tasks d JOIN listings l ON l.id=d.listing_id "
                   "WHERE l.platform_id='ebay'").fetchone()["id"]
    db.execute("UPDATE delist_tasks SET status='done' WHERE id=?", (t,))      # she removed eBay already
    db.execute("UPDATE listings SET status='ended' WHERE item_id=? AND platform_id='ebay'", (iid,))
    db.commit()
    res = sr.void_sale(db, iid, "buyer cancelled")
    assert res.removed == ["ebay"] and res.still_up == ["facebook"], res
    assert db.execute("SELECT voided_at FROM sales WHERE id=?", (sale.sale_id,)).fetchone()[0]
    assert db.execute("SELECT status FROM items WHERE id=?", (iid,)).fetchone()[0] == "listed"
    assert db.execute("SELECT status FROM listings WHERE item_id=? AND platform_id='poshmark'", (iid,)).fetchone()[0] == "live"
    assert all(s == "done" for s in tasks(db, iid).values())
    sr.record_sale(db, iid, "facebook", 40)   # can sell again after an undo


def test_old_database_is_upgraded():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""CREATE TABLE sales (id INTEGER PRIMARY KEY, item_id INTEGER, listing_id INTEGER,
        sale_price REAL NOT NULL, fees REAL, label_cost REAL, net_profit REAL, sold_at TEXT);
        CREATE TRIGGER on_sale_delist_others AFTER INSERT ON sales BEGIN SELECT 1; END;""")
    init(db)
    cols = {r[1] for r in db.execute("PRAGMA table_info(sales)")}
    assert {"source", "voided_at"} <= cols
    sql = db.execute("SELECT sql FROM sqlite_master WHERE name='on_sale_delist_others'").fetchone()[0]
    assert "manual_required" in sql


# ------------------------------------------------------------ email: classify
def test_classify():
    sale = ew.classify(ew.parse(mail("Congratulations! You've made a sale!")), RULES)
    assert sale.platform == "poshmark"
    for subj in ("Jane liked your listing", "You received an offer from jane", "Price drop on items you liked",
                 "Items similar to ones you sold"):
        assert ew.classify(ew.parse(mail(subj)), RULES).platform is None, subj
    eb = ew.classify(ew.parse(mail("You made the sale for Gorham Chantilly Sterling", sender="eBay <ebay@ebay.com>")),
                     RULES)
    assert eb.platform == "ebay"
    assert ew.classify(ew.parse(mail("You made a sale!", sender="spam@example.com")), RULES).platform is None


# ------------------------------------------------------------ email: match
def test_match_by_sku_link_and_title():
    db = fresh_db()
    iid = add_item(db)
    add_item(db, "S0043", "Vintage Coach Willis Bag Black Leather 9927", pm_id="a" * 24, ebay_id="998877665544")
    e = ew.classify(ew.parse(mail("You made a sale!", "Item: fork... SKU: S0042  Sold for $48.00")), RULES)
    m = ew.match(db, e)
    assert (m.how, m.item["id"]) == ("sku", iid) and m.strong
    assert ew.find_price(e) == 48.0

    html = f'<a href="https://click.poshmark.com/r?u=https%3A%2F%2Fposhmark.com%2Flisting%2Ffork-{PM_ID}">view</a>'
    e = ew.classify(ew.parse(mail("You made a sale!", "Your item sold.", html=html)), RULES)
    m = ew.match(db, e)
    assert (m.how, m.item["id"]) == ("link", iid), m

    e = ew.classify(ew.parse(mail("You made the sale for Gorham Chantilly Sterling Silver Dinner Fork No Mon...",
                                  "Order total: $52.10", sender="eBay <ebay@ebay.com>")), RULES)
    m = ew.match(db, e)
    assert (m.how, m.item["id"]) == ("title", iid) and not m.strong, m
    assert ew.find_price(e) == 52.10

    e = ew.classify(ew.parse(mail("You made a sale!", "Something else entirely")), RULES)
    assert ew.match(db, e).item is None


def test_ebay_item_number_in_text():
    db = fresh_db()
    iid = add_item(db)
    e = ew.classify(ew.parse(mail("You made the sale!", "Item number: 123456789012", sender="ebay@ebay.com")), RULES)
    m = ew.match(db, e)
    assert (m.how, m.item["id"]) == ("link", iid), m


# ------------------------------------------------------------ IMAP (faked)
class FakeIMAP:
    boxes = {}

    def __init__(self, host, port, timeout=None):
        self.msgs = FakeIMAP.boxes
        self.readonly = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, pw):
        if pw != "good":
            raise ew.imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")
        return "OK", [b"ok"]

    def select(self, folder, readonly=False):
        self.readonly = readonly
        assert readonly, "watcher must open the mailbox read-only"
        return "OK", [str(len(self.msgs)).encode()]

    def search(self, charset, *crit):
        dom = crit[-1].strip('"')
        hits = [n for n, (frm, _) in self.msgs.items() if dom in frm]
        return "OK", [" ".join(hits).encode()]

    def fetch(self, n, what):
        n = n.decode() if isinstance(n, bytes) else n
        assert "PEEK" in what, "must not mark emails read"
        raw = self.msgs[n][1]
        if "HEADER.FIELDS" in what:
            mid = [line for line in raw.split(b"\n") if line.lower().startswith(b"message-id")][0]
            return "OK", [(b"1", mid + b"\r\n\r\n")]
        return "OK", [(b"1", raw)]


def test_fetch_is_read_only_and_skips_seen():
    ew.imaplib.IMAP4_SSL = FakeIMAP
    FakeIMAP.boxes = {"1": ("poshmark.com", mail("You made a sale!", mid="<one@x>")),
                      "2": ("poshmark.com", mail("Jane liked your listing", mid="<two@x>")),
                      "3": ("ebay.com", mail("You made the sale for X", sender="ebay@ebay.com", mid="<three@x>"))}
    cfg = ew.ImapConfig("a@b.c", "good")
    got = ew.fetch_emails(cfg, RULES, datetime.now(timezone.utc) - timedelta(days=1), {"<three@x>"})
    assert [(e.message_id, e.platform) for e in got] == [("<one@x>", "poshmark"), ("<two@x>", None)], got
    try:
        ew.fetch_emails(ew.ImapConfig("a@b.c", "bad"), RULES, datetime.now(timezone.utc), set())
    except ew.imaplib.IMAP4.error:
        pass
    else:
        raise AssertionError("login failure must raise so the bot can alert")


# ------------------------------------------------------------ bot side (Discord faked)
class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kw):
        self.sent.append(content or "")


class FakeBot:
    def __init__(self, ch):
        self.ch, self.delist_calls = ch, 0

    def get_channel(self, _):
        return self.ch

    async def post_delist_alerts(self):
        self.delist_calls += 1


def _watch(db, auto):
    import sales_watch as sw
    ch = FakeChannel()
    sw.db, sw.bot, sw.alerts_ch = db, FakeBot(ch), 1
    sw.rules["settings"]["auto_record"] = auto
    sw.state.update(failing_since=None, last_fail_ping=None, last_ok=None, error=None)
    return sw, ch


def test_watcher_auto_records_strong_match_and_flags_double_sale():
    db = fresh_db()
    iid = add_item(db)
    sw, ch = _watch(db, auto=True)
    e = ew.classify(ew.parse(mail("You made a sale!", "SKU: S0042 Sold for $48.00", mid="<s1@x>")), RULES)
    asyncio.run(sw.handle(e))
    assert db.execute("SELECT source FROM sales").fetchone()[0] == "email"
    assert set(tasks(db, iid)) == {"ebay", "facebook"} and sw.bot.delist_calls == 1
    assert "@here" in ch.sent[-1] and "S0042" in ch.sent[-1]
    # Same sale, another email from the same site: quietly ignored.
    asyncio.run(sw.handle(ew.classify(ew.parse(mail("You made a sale!", "SKU: S0042", mid="<s2@x>")), RULES)))
    assert len(ch.sent) == 1
    # eBay says it sold too: double-sale alarm.
    asyncio.run(sw.handle(ew.classify(ew.parse(mail("You made the sale for fork", "SKU: S0042",
                                                     sender="ebay@ebay.com", mid="<s3@x>")), RULES)))
    assert "double sale" in ch.sent[-1].lower()


def test_watcher_asks_first_when_auto_off_and_alerts_unmatched():
    db = fresh_db()
    add_item(db)
    sw, ch = _watch(db, auto=False)
    asyncio.run(sw.handle(ew.classify(ew.parse(mail("You made a sale!", "SKU: S0042", mid="<c1@x>")), RULES)))
    assert db.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 0
    assert db.execute("SELECT status FROM email_events WHERE message_id='<c1@x>'").fetchone()[0] == "needs_confirm"
    assert "Did **S0042**" in ch.sent[-1] and "eBay" in ch.sent[-1]
    asyncio.run(sw.handle(ew.classify(ew.parse(mail("You made a sale!", "mystery", mid="<c2@x>")), RULES)))
    assert "couldn't match" in ch.sent[-1]
    asyncio.run(sw.handle(ew.classify(ew.parse(mail("Jane liked your listing", mid="<c3@x>")), RULES)))
    assert db.execute("SELECT status FROM email_events WHERE message_id='<c3@x>'").fetchone()[0] == "ignored"
    assert len(ch.sent) == 2


def test_watcher_alerts_when_inbox_fails_and_when_it_recovers():
    db = fresh_db()
    sw, ch = _watch(db, auto=False)
    ew.imaplib.IMAP4_SSL = FakeIMAP
    FakeIMAP.boxes = {}
    import os
    os.environ.update(EMAIL_ADDRESS="a@b.c", EMAIL_APP_PASSWORD="bad")
    assert asyncio.run(sw.poll_once()) == -1
    assert "Can't check the sales inbox" in ch.sent[-1] and "@here" in ch.sent[-1]
    asyncio.run(sw.poll_once())
    assert len(ch.sent) == 1          # no spam: re-pings only every 2 hours
    os.environ["EMAIL_APP_PASSWORD"] = "good"
    assert asyncio.run(sw.poll_once()) == 0
    assert "readable again" in ch.sent[-1]


# ------------------------------------------------------------ ops: to ship, backup, export
def test_to_ship_backup_and_export(tmp=None):
    import tempfile
    import ops
    db = fresh_db()
    iid = add_item(db)
    db.execute("UPDATE items SET cost_basis=3, category='jewelry', bin_id=NULL WHERE id=?", (iid,))
    fb = add_item(db, "S0050", "Something local", pm_id="b" * 24, ebay_id="111111111111")
    sr.record_sale(db, iid, "poshmark", 48)
    sr.record_sale(db, fb, "facebook", 20)
    db.execute("UPDATE sales SET sold_at=datetime('now','-10 days') WHERE item_id=?", (iid,))
    db.commit()
    ship = ops.to_ship(db)
    assert [x["sku"] for x in ship] == ["S0042"] and ship[0]["overdue"]      # Facebook pickups aren't shipped
    assert "OVERDUE" in ops.ship_lines(ship)[0]

    text, n, total = ops.sales_csv(db, int(db.execute("SELECT strftime('%Y', sold_at) FROM sales").fetchone()[0]))
    assert n == 2 and total == 68.0
    posh = [r for r in text.splitlines() if "S0042" in r][0].split(",")
    assert posh[5] == "9.60" and posh[7] == "3.00", posh                    # 20% Poshmark fee, cost basis
    sr.void_sale(db, fb)
    assert ops.sales_csv(db, int(posh[0][:4]))[1] == 1                      # undone sales are left out

    with tempfile.TemporaryDirectory() as d:
        ops.BACKUPS = Path(d) / "backups"
        off = Path(d) / "off"
        off.mkdir()
        import os
        os.environ["BACKUP_OFFSITE"] = str(off)
        written = ops.backup(db)
        copy = sqlite3.connect(written[0])
        assert copy.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 2
        assert (off / written[0].name).exists()
        os.environ["BACKUP_OFFSITE"] = str(Path(d) / "missing")
        try:
            ops.backup(db)
        except RuntimeError:
            pass
        else:
            raise AssertionError("a missing offsite folder must fail loudly")
        del os.environ["BACKUP_OFFSITE"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
