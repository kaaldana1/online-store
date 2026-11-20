# Platform rules

The seller lists the same item on eBay (shipped), Poshmark (shipped) and Facebook
Marketplace (local pickup only). One description serves all three, so write it to be
safe everywhere. Titles and fields are per platform.

## eBay

- **Title**: 80 characters maximum. Aim for 65–80. Brand or maker first, then pattern,
  style or model, then item type, then attributes. eBay's search matches title words
  and filters on item specifics, so the two together decide whether a buyer ever sees
  the listing.
- **Item specifics**: these power the filters down the left side of search. Fill every
  one you can support. Use `name` exactly as eBay spells it (see the category
  reference, or the `ebay_aspects` list if the request includes one). Values should
  be short and standard ("925 Parts per Thousand" style values are fine if you know
  them; otherwise "Sterling Silver").
- **Don't**: name brands the item isn't ("like Tiffany", "not Coach", "Pandora
  compatible" unless it's genuinely a compatible charm and you say "compatible with"),
  add unrelated keywords, or use hype ("L@@K", "WOW", "must see"). eBay treats
  keyword padding as search manipulation.
- No links, phone numbers, emails or mentions of other selling sites.

## Poshmark

- **Title**: 80 characters maximum, but the feed on phones shows only the start, so the
  first few words carry the most weight. Lead with item type plus the most distinctive
  attribute; brand is also shown separately, but include it when space allows because
  shoppers type it.
- **Brand**: spell it the way Poshmark's brand list does ("Coach", "Eileen Fisher",
  "Reed & Barton"). Brand filters and brand follows rely on an exact match. Use
  "Unbranded" or "Vintage" when there's no brand.
- **Category**: department > category > subcategory, e.g. `Women > Bags > Shoulder Bags`,
  `Women > Jewelry > Bracelets`, `Home > Kitchen > Flatware`.
- **Size**: the tagged size for clothing and shoes, ring size for rings, "OS" (one size)
  for bags and most jewelry.
- **Colors**: up to two, from basic color names (Black, Brown, Tan, Silver, Gold, Red,
  Blue, Green, Cream, White, Pink, Purple, Orange, Yellow, Gray).
- **Style tags**: up to three short phrases a shopper might search that aren't already
  in the title ("boho", "statement", "minimalist", "workwear", "mid century").
- Never mention other platforms, outside payment (Venmo, PayPal, Zelle, Cash App),
  trades, or meeting up. Poshmark removes listings that point buyers off the site.
- Poshmark's audience is mostly fashion shoppers. Home items such as flatware sell
  there, but slower; that's a pricing and platform decision, not yours, so write the
  Poshmark fields anyway.

## Facebook Marketplace

- **Title**: keep it under 100 characters and readable. Local buyers type plain words,
  so pair the collector term with the everyday one: "Sterling Silver Serving Spoon –
  Gorham Chantilly", "Coach Leather Shoulder Bag – Vintage Made in USA".
- **Tags**: up to ten everyday search words (no '#').
- The bot adds pickup information to the Facebook packet, so don't put pickup details,
  addresses or payment methods in the shared description.

## The shared description

Plain text, no HTML, no emoji, under 1,200 characters, in this order:

1. One line saying what it is, in full words.
2. Details: maker or brand, marks seen, material and purity, era, measurements,
   weight (grams for silver and jewelry), size.
3. Condition: specific and honest. Name each flaw and where it is ("small dent near
   the tip, see photo 4"). Normal wear is "light wear consistent with age"; don't
   bury real damage in that phrase.
4. What's included (dust bag, box, chain, original tags).
5. `SKU: <sku>` as the last line, if a SKU was given. This lets the team find the
   listing quickly when it needs to come down.

If a measurement isn't visible, write `Measurements: ___` so the seller fills it in.

## Words and claims to avoid

| Avoid | Why | Instead |
|---|---|---|
| "authentic", "genuine" without strong evidence | Returns and counterfeit strikes | Describe the evidence ("creed patch reads No. ...") |
| "sterling", "solid silver" on plate | Misdescription | "silverplate", "silver plated" |
| "gold" alone on plated, filled, vermeil or "gold tone" | Misdescription | "gold filled", "gold plated", "gold tone" |
| "Native American", tribe names, "Indian made" without documentation | Indian Arts and Crafts Act | "Southwestern style" |
| "diamond", "ruby", "natural" for untested stones | Can't verify from photos | "clear stone", "red stone", "untested" |
| "rare", "HTF", "museum quality" | Hype; erodes trust | Concrete facts (maker, date, pattern) |
| "smoke-free home", "pet-free" | You can't know it | Leave out unless the notes say so |
| other brands, "inspired by", "dupe", "replica" | Keyword spam or counterfeit signal | Describe the item itself |

## Before you submit

- Every claim in a title is backed by `evidence`.
- Titles fit their limits and start with the strongest search words.
- Plate, filled and tone metals are labeled as such everywhere.
- Description follows the order above, contains no other platform names and no
  outside payment methods, and ends with the SKU line when one was given.
- Poshmark has at most three style tags and two colors; Facebook at most ten tags.
- `checks_before_listing` names the specific photo or measurement still needed.
- `blockers` is filled if anything in `item-checks.md` says not to list.
