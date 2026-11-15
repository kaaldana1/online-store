# Platform research: eBay, Facebook Marketplace, Poshmark

Researched 2026-11-11. Numbers the code uses live in `fees.toml`; this file explains them.
**Verify in the app** marks figures that came only from cross-lister blogs, because the official page blocked automated reading.

## At a glance

| | eBay | Facebook Marketplace | Poshmark |
|---|---|---|---|
| Seller fee | 13.6% most categories, **15% jewelry**, plus $0.30/$0.40 per order | **0% local pickup**; 5% shipped ($0.40 min ≤ $8) | **$2.95 under $15; 20% at $15+** |
| Fee charged on | item **+ shipping + sales tax** | item price only | item price only |
| Who pays shipping | You set it (buyer-paid or free) | **We do local pickup only** (owner decision) | Buyer pays $6.49 label up to 5 lb; +$5 for 5–10 lb, +$10 over 10 lb |
| Listing cost | 250 free/month, then $0.35 | Free | Free |
| Official API | **Yes**: list, revise, end, messages, best offers, sold notifications | **No** for individual sellers | **No** |
| Automation stance | Allowed through the API | Bot posting risks the account | Community Guidelines **ban** bots for sharing, relisting, liking, following |
| How we'll handle it | Fully automated after approval | Bot prepares a copy-paste packet, human posts | Bot prepares a copy-paste packet, human posts and shares |

**What this means for pricing:** Facebook local pickup keeps the most money, then Facebook shipped. eBay and Poshmark take about the same cut at typical thrift prices. Poshmark's flat $2.95 is brutal on items under ~$10, so the pricer should steer cheap items away from it. Example output of `pricing/fees.py` for a $40 sterling bracelet bought for $3 with a $5.50 label: Facebook (local) nets $37.00, eBay $29.23, Poshmark $29.00. Heavy items hurt on Poshmark: at 7 lb the surcharge drops it to $24.00.

## eBay (official help pages)
- Final value fee: 13.6% up to $7,500 in most categories, 2.35% above that. **Jewelry & Watches is 15%** up to $5,000 (9% above). Watches are 15% up to $1,000. Coins are 13.25%. Bullion is 13.6% (7% over $7,500). Per-order fee: $0.30 if the order is ≤ $10, otherwise $0.40.
- The fee applies to the **whole order including shipping and sales tax**, so free shipping isn't cheaper for you than charging shipping.
- Insertion: 250 free listings a month, then $0.35. Good 'Til Cancelled listings count again each monthly renewal.
- Promoted Listings (general): you pick 2–100% of the sale total. You pay only if the item sells within 30 days of an ad click. This is the lever the future health checker can propose.
- Stores: Starter ($4.95/mo yearly) doesn't lower fees. Basic ($21.95/mo yearly) drops most categories to 12.7% and jewelry to 13%, and gives 1,000 free fixed-price listings. At 5/day (~150/month) Basic is roughly break-even on jewelry and worth revisiting once sales start.
- APIs: the Inventory API and Trading API create, revise and end listings. The Message API reads and sends buyer messages. Trading `GetBestOffers` / `RespondToBestOffer` handle offers, and the Negotiation API sends offers to watchers. The Notification API can push events. Default limits (5,000 Trading calls/day) are far above our needs.
- **Sold-price comps:** eBay's Marketplace Insights (sold data) API is **limited release, approved partners only**. The Browse API (active listings) needs a Buy API license approval. Step 2 has to plan around this (options: apply for Browse, manual Terapeak lookups, or other comp sources).
- Out-of-stock sales can lead to defects and account restrictions, which is why the database auto-creates delist tasks on every sale.

Sources: [Selling fees](https://www.ebay.com/help/selling/fees-credits-invoices/selling-fees?id=4822) · [Store fees](https://www.ebay.com/help/selling/selling-fees/store-fees?id=4809) · [Promoted Listings](https://www.ebay.com/help/selling/listings/listing-tips/promoted-listings?id=4164) · [API call limits](https://developer.ebay.com/develop/api/sell/api_call_limits) · [Message API](https://developer.ebay.com/develop/api/sell/message_api) · [Marketplace Insights](https://developer.ebay.com/api-docs/buy/marketplace-insights/static/overview.html)

## Facebook Marketplace (secondary sources, verify in the app)
- Local pickup: no fees at all. Shipped: 5% of the item price (includes payment processing), $0.40 flat for orders ≤ $8. No listing fees.
- Payout: roughly 1–5 business days after delivery is confirmed (vendor-reported).
- **Unconfirmed:** whether shipping is still offered to all individual US sellers, and the exact jewelry and precious-metal rules. Check Meta's Commerce Policies once in the app before the first listing.
- No posting or messaging API for individual sellers, and major cross-listers say they don't support it. Treat it as manual: the bot writes the listing, a human posts and chats.

Sources: [Voolist FB fees 2026](https://www.voolist.com/blog/facebook-marketplace-fees-2026) · [Closo FB fees](https://closo.co/blogs/platform-specific-guides/facebook-marketplace-fees). facebook.com blocked automated reading.

## Poshmark (secondary sources for fees; official Terms and Guidelines for policy)
- Fees: $2.95 flat under $15, 20% at $15 and up, on the item price only. The fee covers payment processing.
- Shipping: the buyer pays a prepaid USPS Priority label up to 5 lb. You pay the upgrade if it's heavier. The label price is **$7.67 or $8.27** depending on the source (verify in the app). Shipping discounts you offer come out of your earnings.
- Offers: an accepted offer is a binding sale. The buyer can cancel only within 3 hours. "Offer to likers" lets you send a discount to everyone who liked the item.
- Jewelry and sterling: suspected counterfeits get the order cancelled with no payout. Photograph hallmarks and stamps (e.g. "925") on every piece.
- **Automation:** the Community Guidelines forbid "unauthorized programs or other forms of automation", including deleting and reposting, liking, sharing and following. The Terms also forbid scraping. Cross-listing itself isn't banned, but sales must be completed on Poshmark. So the bot drafts and a human does everything on Poshmark.

Sources: [Vendoo Poshmark fees](https://blog.vendoo.co/everything-you-need-to-know-about-poshmark-selling-fees) · [Voolist Poshmark fees 2026](https://www.voolist.com/blog/poshmark-fees-2026) · [Poshmark Terms](https://poshmark.com/terms) · [Community Guidelines](https://poshmark.com/community_guidelines)

## Checked by the owner in-app (2026-11-15)
1. Poshmark label: **$6.49** up to 5 lb, +$5 for 5.01–10 lb, +$10 for 10.01 lb and up. The code assumes the surcharge comes out of the seller's earnings; confirm on the first heavy sale.
2. Facebook: shipping is optional and pricier, so **local pickup only**. No jewelry-specific rules.
3. eBay developer account: applied, **under review**. Request Browse API access once it's approved (needed for comps in step 2).
