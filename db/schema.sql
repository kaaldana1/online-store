-- Unified store database. SQLite for now (one file, no server, $0). It is written
-- to port to Postgres later with minimal changes. Run: python db/init_db.py
PRAGMA foreign_keys = ON;

-- ------------------------------------------------------------ platforms
CREATE TABLE IF NOT EXISTS platforms (
    id          TEXT PRIMARY KEY,              -- 'ebay', 'facebook', 'poshmark', ...
    name        TEXT NOT NULL,
    priority    INTEGER NOT NULL DEFAULT 2,
    automation  TEXT NOT NULL CHECK (automation IN ('api','manual')),
    enabled     INTEGER NOT NULL DEFAULT 1
);

-- ------------------------------------------------------------ storage
-- Bins are rectangles on the storage map drawing. x/y/w/h are in grid units, so the
-- drag-and-drop UI can be reconfigured without touching item rows.
CREATE TABLE IF NOT EXISTS bins (
    id      INTEGER PRIMARY KEY,               -- the number painted on the bin
    label   TEXT,
    x REAL NOT NULL DEFAULT 0, y REAL NOT NULL DEFAULT 0,
    w REAL NOT NULL DEFAULT 1, h REAL NOT NULL DEFAULT 1,
    notes   TEXT
);

-- ------------------------------------------------------------ items
CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY,
    sku           TEXT UNIQUE NOT NULL,         -- e.g. 'J-0042' (shown on every Discord message)
    name          TEXT,                         -- short internal name, e.g. 'golden chalice, vine engraving'
    description   TEXT,                         -- free text; also embedded for the finder (RAG)
    category      TEXT,                         -- matches fees.toml category keys ('jewelry', ...)
    condition     TEXT,
    cost_basis    REAL DEFAULT 0,               -- what was paid at the thrift store
    weight_oz     REAL,
    dims_in       TEXT,                         -- 'LxWxH'
    sourced_at    TEXT,
    sourced_where TEXT,
    bin_id        INTEGER REFERENCES bins(id),
    status        TEXT NOT NULL DEFAULT 'sourced'
                  CHECK (status IN ('sourced','appraised','drafted','listed','sold','shipped','archived')),
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS item_photos (
    id         INTEGER PRIMARY KEY,
    item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    path       TEXT NOT NULL,
    is_primary INTEGER NOT NULL DEFAULT 0
);

-- Embeddings for the "which bin is the golden chalice in?" finder. The vector is stored
-- as a blob so we can use sqlite-vec later, or move to pgvector.
CREATE TABLE IF NOT EXISTS item_embeddings (
    item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    model   TEXT NOT NULL,
    vector  BLOB NOT NULL
);

-- ------------------------------------------------------------ research
CREATE TABLE IF NOT EXISTS appraisals (
    id           INTEGER PRIMARY KEY,
    item_id      INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    price_low    REAL, price_median REAL, price_high REAL,
    comp_count   INTEGER,
    comps_json   TEXT,                          -- raw comps: title, price, sold/active, url, platform
    keywords     TEXT,                          -- JSON array of keywords mined from comps
    method       TEXT,                          -- 'ebay_browse', 'manual', 'sandbox', ...
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ------------------------------------------------------------ listings (one per item per platform)
CREATE TABLE IF NOT EXISTS listings (
    id            INTEGER PRIMARY KEY,
    item_id       INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    platform_id   TEXT NOT NULL REFERENCES platforms(id),
    status        TEXT NOT NULL DEFAULT 'draft'
                  CHECK (status IN ('draft','pending_approval','approved','live','ended','sold','delist_failed')),
    external_id   TEXT,                         -- platform's listing id once posted
    url           TEXT,
    title         TEXT,
    description   TEXT,
    tags          TEXT,                         -- JSON array
    list_price    REAL,
    floor_price   REAL,                         -- lowest offer the seller will consider
    est_net       REAL,                         -- net profit at list_price (from pricing/fees.py)
    posted_at     TEXT,
    ended_at      TEXT,
    last_synced   TEXT,
    UNIQUE (item_id, platform_id)
);

-- ------------------------------------------------------------ buyer conversations
-- Nested as item -> platform -> buyer:
--   listing (item+platform)  1 ─── *  buyer_threads (one per buyer)  1 ─── *  messages / offers
CREATE TABLE IF NOT EXISTS buyer_threads (
    id                 INTEGER PRIMARY KEY,
    listing_id         INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    buyer_handle       TEXT NOT NULL,
    platform_thread_id TEXT,                    -- conversation id on the platform (if API)
    discord_thread_id  TEXT,                    -- the Discord thread mirroring it
    status             TEXT NOT NULL DEFAULT 'open'
                       CHECK (status IN ('open','waiting_seller','waiting_buyer','accepted','declined','closed')),
    sentiment          TEXT,                    -- 'friendly' | 'neutral' | 'frustrated' (set by merchant bot)
    last_buyer_msg_at  TEXT,
    last_seller_msg_at TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (listing_id, buyer_handle)
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY,
    thread_id   INTEGER NOT NULL REFERENCES buyer_threads(id) ON DELETE CASCADE,
    direction   TEXT NOT NULL CHECK (direction IN ('in','out')),
    body        TEXT NOT NULL,
    is_draft    INTEGER NOT NULL DEFAULT 0,     -- bot-drafted replies wait for approval
    sent_at     TEXT
);

CREATE TABLE IF NOT EXISTS offers (
    id          INTEGER PRIMARY KEY,
    thread_id   INTEGER NOT NULL REFERENCES buyer_threads(id) ON DELETE CASCADE,
    amount      REAL NOT NULL,
    est_net     REAL,                           -- net profit if accepted, given this platform's fees
    status      TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open','countered','accepted','declined','expired','withdrawn')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at  TEXT
);

-- Every open offer on an item across ALL platforms, ranked by what the seller actually keeps.
CREATE VIEW IF NOT EXISTS best_offers AS
SELECT i.sku, i.name, l.platform_id, t.buyer_handle, t.sentiment,
       o.amount, o.est_net, o.created_at, o.expires_at, o.id AS offer_id, i.id AS item_id
FROM offers o
JOIN buyer_threads t ON t.id = o.thread_id
JOIN listings l      ON l.id = t.listing_id
JOIN items i         ON i.id = l.item_id
WHERE o.status = 'open'
ORDER BY i.id, o.est_net DESC;

-- ------------------------------------------------------------ approvals (human-in-the-loop)
-- Anything that needs your mom's OK goes through here: listings, price changes, replies, offers.
CREATE TABLE IF NOT EXISTS approvals (
    id                 INTEGER PRIMARY KEY,
    kind               TEXT NOT NULL CHECK (kind IN ('post_listing','price_change','buyer_reply','accept_offer','promote')),
    ref_table          TEXT NOT NULL,
    ref_id             INTEGER NOT NULL,
    summary            TEXT NOT NULL,           -- one line shown in Discord
    payload            TEXT,                    -- JSON with the exact change to apply
    status             TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending','approved','rejected','expired','applied','failed')),
    discord_message_id TEXT,
    requested_at       TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at         TEXT,
    decided_by         TEXT
);

-- ------------------------------------------------------------ sales + cross-platform safety
CREATE TABLE IF NOT EXISTS sales (
    id          INTEGER PRIMARY KEY,
    item_id     INTEGER NOT NULL REFERENCES items(id),
    listing_id  INTEGER NOT NULL REFERENCES listings(id),
    sale_price  REAL NOT NULL,
    fees        REAL,
    label_cost  REAL,
    net_profit  REAL,
    sold_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- When an item sells, one delist task is created per OTHER live listing. The sync worker
-- runs them; anything that fails or needs a manual step pings #alerts until it's done.
CREATE TABLE IF NOT EXISTS delist_tasks (
    id          INTEGER PRIMARY KEY,
    listing_id  INTEGER NOT NULL REFERENCES listings(id),
    reason_sale INTEGER REFERENCES sales(id),
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','done','failed','manual_required')),
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    done_at     TEXT
);

CREATE TRIGGER IF NOT EXISTS on_sale_delist_others
AFTER INSERT ON sales
BEGIN
    UPDATE listings SET status = 'sold' WHERE id = NEW.listing_id;
    UPDATE items    SET status = 'sold' WHERE id = NEW.item_id;
    INSERT INTO delist_tasks (listing_id, reason_sale, status)
        SELECT l.id, NEW.id,
               CASE p.automation WHEN 'api' THEN 'pending' ELSE 'manual_required' END
        FROM listings l JOIN platforms p ON p.id = l.platform_id
        WHERE l.item_id = NEW.item_id AND l.id <> NEW.listing_id
          AND l.status IN ('live','approved','pending_approval');
END;

-- ------------------------------------------------------------ gateway: agent runs + cost
CREATE TABLE IF NOT EXISTS agent_runs (
    id            INTEGER PRIMARY KEY,
    agent         TEXT NOT NULL,                -- 'appraiser', 'lister', 'merchant', ...
    task          TEXT,
    model         TEXT,
    input_tokens  INTEGER, output_tokens INTEGER,
    cost_usd      REAL,
    status        TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','ok','failed','retrying')),
    error         TEXT,
    started_at    TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at   TEXT
);

CREATE VIEW IF NOT EXISTS spend_this_month AS
SELECT agent, COUNT(*) AS runs, ROUND(SUM(cost_usd), 2) AS usd
FROM agent_runs
WHERE started_at >= date('now', 'start of month')
GROUP BY agent;

-- Daily listing goal (5/day) tracking.
CREATE VIEW IF NOT EXISTS listings_per_day AS
SELECT date(posted_at) AS day, platform_id, COUNT(*) AS n
FROM listings WHERE posted_at IS NOT NULL
GROUP BY day, platform_id ORDER BY day DESC;

-- ------------------------------------------------------------ step 3: finder index
-- Rebuilt from items/listings/appraisals before each search (cheap at thrift-store scale).
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(item_id UNINDEXED, body, tokenize = 'porter unicode61');
