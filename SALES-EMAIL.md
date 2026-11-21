# Sale detection from email

When something sells, the marketplace emails you. The bot reads those emails and makes sure
the item comes down everywhere else. It doesn't use AI and never logs in to a marketplace. It
only reads your inbox, read-only: emails are never marked read, moved or deleted.

```
sale email (eBay / Poshmark / Mercari / Depop)
   → bot checks the inbox every 3 minutes (sales/email_watch.py)
   → which item? SKU in the email → link to the listing → listing title
   → #alerts: "Did S0042 sell on Poshmark for $48?"  [Yes, it sold: record it] [Not a sale]
   → Yes → sale recorded → red delist alert for every other site, with the link and the exact steps
```

Facebook Marketplace cash pickups send no email, so record those with `/sold`.

## What you'll see in #alerts

| Card | Meaning | What to do |
|---|---|---|
| 📧 Did **S0042** sell on Poshmark? | A sale email matched an item | Tap **Yes** (the delist alerts follow) or **Not a sale** |
| 💰 S0042 sold on Poshmark | Recorded automatically (only when `auto_record = true`) | Remove it from the other sites listed in the delist alerts |
| 📧 A Poshmark sale email I couldn't match | It's a sale, but the item isn't clear | `/sold <sku> poshmark <price>`, then tap **Handled it** |
| 🚨 Possible double sale | The item was already recorded as sold somewhere else | Check both orders before shipping and cancel one |
| 📧❌ Can't check the sales inbox | Wrong password, network, Gmail down... | Fix it; until then use `/sold`. It re-pings every 2 hours |

Unanswered cards are re-posted with @here every 2 hours. `/inbox` shows the watcher's state
and checks the inbox right away. `/unsold S0042` undoes a sale (buyer cancelled within
Poshmark's window, a return, or a Facebook no-show).

## Set it up (about 5 minutes)

1. Use the email address the marketplaces send sale emails to. If you use different addresses
   for different sites, the watcher only sees the one you set here.
2. **Gmail:** turn on 2-Step Verification, then go to Google Account → Security → **App passwords**
   and create one named "store bot". You get 16 letters. Other providers: look for "app password"
   or "IMAP" in their settings; set `EMAIL_IMAP_HOST` too.
3. In `~/store-system/bot/.env` add:
   ```
   EMAIL_ADDRESS=you@gmail.com
   EMAIL_APP_PASSWORD=abcd efgh ijkl mnop
   ```
4. Check it without changing anything:
   ```bash
   docker-compose up -d --build
   docker-compose exec storebot python sales/email_watch.py --dry-run --days 30
   ```
   This lists every marketplace email from the last 30 days and whether it counts as a sale.
   Not selling yet? Expect only `ignore` lines (likes, offers, newsletters).
5. In Discord, run `/inbox`. It should say ✅ last checked.

## Make matching reliable

- **Paste the listing link when you tap Mark posted.** The watcher finds the listing ID in the email.
- **Keep the `SKU: S0042` line at the end of every description** (the drafts already have it), and on
  eBay also put the SKU in the **Custom label (SKU)** field. eBay's sale emails show it.
- Matches by title alone always ask before recording.

## Auto-record

`sales/email_rules.toml` has `auto_record = false`, so every sale email asks first. After a couple
of weeks of correct cards, set it to `true` and restart. Then emails that name the SKU or link
the exact listing are recorded immediately, with an @here, and the delist alerts go out
right away. Title-only matches still ask.

## When an email is missed or wrongly caught

Each site has a sender and two subject patterns in `sales/email_rules.toml`. Save the email
(Gmail: ⋮ → **Download message**) and test it:

```bash
docker-compose exec storebot python sales/email_watch.py --file /app/data/sale.eml
```

(copy the .eml into `~/store-system/data/` first). Adjust the pattern, rebuild, and test again.

## Why not delist automatically?

Poshmark's Community Guidelines ban unauthorized automation, and Meta's rules for Facebook are
similar. After our script test, Poshmark hid the test listings. A flagged closet costs far more
than tapping a button, so the bot detects sales automatically and people do the delisting. The
old browser worker is kept, unused, on the `archive/browser-delist-automation` branch. eBay can be
automated through its official API once the developer account is approved.
