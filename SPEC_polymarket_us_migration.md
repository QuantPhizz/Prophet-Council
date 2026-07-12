# SPEC: Migrate Prophet Council from Polymarket International (read) to Polymarket US (read + write)

Audience: Claude Code terminal agent, working in `~/tko-agents/Prophet Council/` on `develop`.

## Why this exists
The operator trades on **Polymarket US** (`polymarket.us`), a separate CFTC-regulated
platform from Polymarket International (`polymarket.com`). The current codebase reads
market data from International's public Gamma/CLOB endpoints. This is a real bug, not
cosmetic: International and US have different order books, different prices, and
different markets, even when the questions look identical (e.g. a BTC-$150k market
exists independently on both platforms with unrelated pricing). Before any live
trading, BOTH the data-read layer and the order-execution layer must point at
Polymarket US — otherwise the desk will size and time trades off prices that don't
exist on the platform it's actually trading against.

## Guardrails (carry forward from prior handoffs)
- Do not touch existing storm team aliases, other `~/.claude/agents` entries, or
  unrelated crontab lines.
- Do not change `RISK` parameters, agent weights, or persona files as part of this
  migration — this is a data-source/execution swap only. Flag anything that seems to
  require a risk-logic change rather than silently changing it.
- Keep `LIVE_TRADING=false` in `.env` throughout this work. This spec does NOT
  authorize going live — it authorizes building and testing the capability to.
- Commit to `develop`, not `main`, same as every prior change. Operator reviews the
  diff before merging.

## Confirmed facts about the Polymarket US API (verified interactively this session —
## do not re-derive these from the SDK's own doc examples, which are inconsistent
## with the live API in places)

- Install: `pip install polymarket-us`
- Client: `PolymarketUS(key_id=..., secret_key=...)` — no wallet, no private key,
  no Polygon/gas token needed anywhere in this flow.
- `client.markets.list(params)` returns a **dict** `{"markets": [...]}`, not a bare
  list. No pagination metadata (no `total`, `cursor`, `hasMore`) is present in the
  response — you only know you've reached the end when a page returns fewer than
  `limit` markets.
- Confirmed **working** query params on `markets.list`: `limit` (server caps at 500
  per call regardless of what you request), `closed` (bool), `offset` (int — verified
  to actually paginate, since offset=200 returned different results than offset=0).
- Confirmed **non-functional / silently ignored** params: `active`, `category` (as a
  list-filter argument — passing it changes nothing; must filter client-side on the
  returned `category` field instead), `sort`, `order`, `page`. These do not error,
  they are just ignored — don't assume a 200 response means the filter applied.
- Real market object fields (from live payloads, not docs): `id`, `question`, `slug`
  (⚠️ the SDK's own quickstart docs call this `marketSlug` in code examples — that's
  wrong for `markets.list`'s actual output; verify independently whether
  `orders.create` expects `marketSlug` or `slug`, don't assume symmetry with the read
  side), `endDate`, `startDate`, `category`, `description`, `active`, `closed`,
  `marketType`, `marketSides` (array of outcome-level objects with `price`,
  `description`, `long`/short flag, etc.), `outcomes` (JSON-encoded string, e.g.
  `'["Yes","No"]'` — needs `json.loads`), `outcomePrices` (JSON-encoded string of
  floats-as-strings, same parsing need), `tags` (observed empty `[]` on every market
  so far — do not build tag-based filtering on this field), `feeCoefficient`,
  `minimumTradeQty`.
- Real category values observed (this is the actual taxonomy — use these, not
  International's tag vocabulary): `sports`, `politics`, `culture`, `macro`,
  `finance`, `geopolitics`, `technology`, `climate`, `crypto`.
  - **There is no `weather` category. The equivalent is `climate`.**
  - **`crypto` is currently thin** — ~13 open markets total, all Bitcoin-price
    threshold questions, no other assets. Do not assume the same breadth as
    International's crypto markets.
- `client.markets.retrieveBySlug(slug)` and `client.markets.book(slug)` are
  documented but **not yet verified against live data this session** — test these
  explicitly before relying on them (see Testing section).
- Auth is handled internally by the SDK (Ed25519 signing under the hood); you only
  ever pass `key_id`/`secret_key`.

## Part 1 — Data layer swap (read side)

Replace these three functions' *implementation* (keep their signatures/return types
so the rest of the pipeline doesn't need to change):

1. **`fetch_candidate_markets(exclude_ids)`**
   - Page through `client.markets.list({"limit": 500, "closed": False, "offset": N})`
     until a page returns < 500.
   - Filter client-side on `m["category"] in MARKET_TAGS` (see config change below).
   - Filter out anything in `exclude_ids` (currently held positions), same as before.
   - Apply the existing liquidity/token-count checks as best they map — note
     Polymarket US market objects don't have a `liquidityNum`/`clobTokenIds` shape
     like Gamma; find the closest equivalent (likely something inside `marketSides`
     or a volume field) and flag to the operator if no liquidity figure is exposed at
     all, since `min_liquidity_usd` currently depends on one existing.
   - Map fields into the existing `MarketSnapshot` dataclass: `market_id` ← `slug`
     (US markets are slug-keyed, not conditionId-keyed — this changes what
     `state["positions"]` keys look like too; note this as a breaking change from
     existing paper-mode state files, see Migration Notes below), `question`,
     `description`, `end_date` ← `endDate`, and bid/ask from `marketSides` prices
     or from `client.markets.book(slug)` once that endpoint is verified.

2. **`fetch_top_of_book(token_id_or_slug)`**
   - Verify `client.markets.book(slug)` first (see Testing). If it returns real
     bid/ask, use it directly. If it doesn't exist or returns something else, fall
     back to deriving best bid/ask from `marketSides[].price` on the market object
     itself and document which approach was used.

3. **`fetch_market_status(market_id)`**
   - Use `client.markets.retrieveBySlug(slug)` (verify field name first) or
     `markets.list` filtered to that slug. Map `closed` directly. Parse
     `outcomePrices` (JSON string) into a list of floats for `settle_resolved()` to
     consume — this is a different shape than Gamma's `outcomePrices`, confirm the
     parsing still lines up (values look like `'["1","0"]'` — strings, not native
     floats, needs both `json.loads` and `float()`).

## Part 2 — Execution layer swap (write side)

Replace `clob_client()` / `place_order()` entirely:

- Drop `py_clob_client`, `POLY_PRIVATE_KEY`, and `chain_id=137` — none of this
  applies to Polymarket US.
- New client construction:
  ```python
  from polymarket_us import PolymarketUS
  client = PolymarketUS(
      key_id=os.environ["POLYMARKET_KEY_ID"],
      secret_key=os.environ["POLYMARKET_SECRET_KEY"],
  )
  ```
- New order call, shape per SDK docs (verify the slug field name against what
  `markets.list` actually returns before trusting `marketSlug`):
  ```python
  order = client.orders.create({
      "marketSlug": slug,          # CONFIRM: may actually need to be "slug"
      "intent": "ORDER_INTENT_BUY_LONG",   # or SELL/short equivalent for exits
      "type": "ORDER_TYPE_LIMIT",
      "price": {"value": str(limit_price), "currency": "USD"},
      "quantity": shares,
      "tif": "TIME_IN_FORCE_GOOD_TILL_CANCEL",
  })
  ```
- Determine the correct `intent` values for: buying YES, buying NO, and closing/
  selling an existing position. The docs only showed `ORDER_INTENT_BUY_LONG` — find
  the SELL/short equivalent (check `docs.polymarket.us/api-reference` or SDK type
  definitions) before wiring up `execute_exit()`.
- Determine how to read back order status/fill confirmation from the response object
  — `execute_entry`/`execute_exit` currently log `order_response` but don't parse
  fill price/quantity; check whether the real order response includes a fill price
  that may differ from the limit price requested, and whether the journal should
  record actual fill price instead of requested limit price.
- Check whether Polymarket US requires an explicit **redeem** step for resolved
  winning positions (the SDK quickstart mentions "redeem winning positions" as a
  distinct concept from settlement) — if so, `settle_resolved()` may need an
  additional API call, not just a price-based P&L calculation.

## Config changes

- `MARKET_TAGS = ["crypto", "sports", "climate"]` (was `["crypto", "sports",
  "weather"]` — `weather` does not exist as a category on Polymarket US).
- New env vars in `.env` / `.env.example`: `POLYMARKET_KEY_ID`, `POLYMARKET_SECRET_KEY`.
- Remove `POLY_PRIVATE_KEY` from `.env.example` and the HANDOFF/README references —
  it no longer applies.
- Add `polymarket-us` to `requirements.txt`; remove `py-clob-client` if nothing else
  in the project needs it.

## Migration notes / breaking changes to flag explicitly to the operator

- **Position keys change shape.** Existing paper positions are keyed by International
  `conditionId`. Post-migration, new positions will be keyed by US `slug`. These are
  not compatible. Recommend starting from a clean `orchestrator_state.json` after this
  migration lands — do not attempt to carry forward paper positions across the swap.
- **The paper-trading evaluation clock resets again.** Everything analyzed and traded
  so far was against International prices/liquidity. Once this migration lands, the
  desk is analyzing a different market universe (fewer crypto markets, `climate`
  instead of `weather`, different liquidity profile). Treat the next paper run as a
  new day zero, not a continuation.

## Testing requirements before this is considered done

1. Confirm `client.markets.retrieveBySlug()` and `client.markets.book()` work against
   a real open slug (pick one from the live desk's own candidate list) — log the raw
   response shape of each, same way we did for `markets.list`.
2. Dry-run `fetch_candidate_markets` end-to-end and confirm it returns real
   `MarketSnapshot` objects for `crypto`/`sports`/`climate` with non-null bid/ask.
3. Confirm `orders.create`'s expected slug field name and intent values WITHOUT
   placing a real order first — check SDK type stubs/source or sandbox docs if a
   sandbox environment exists; only place a real trial order with explicit operator
   sign-off, at minimal size.
4. Full paper cycle (`FORCE_HUNT=true`, `LIVE_TRADING=false`) against the new data
   layer, confirming the journal shows real Kisuke/Aizen/Gojo opinions on genuine US
   candidate markets.
5. Confirm `settle_resolved()` correctly parses a real closed US market's
   `outcomePrices` and computes P&L as expected on a synthetic test position (same
   pattern as the earlier trailing-take-profit unit tests — test against a temp
   state file, not live state).

## Explicitly out of scope

- Setting `LIVE_TRADING=true`.
- Placing any real order without explicit operator go-ahead on that specific test.
- Any change to `RISK`, agent weights, or persona files.
