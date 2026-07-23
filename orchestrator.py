"""
Multi-Agent Prediction Market Orchestrator — v4
===============================================
Desk:
  - Kisuke  -> Claude Fable 5    (Vertex AI, global) — weight 1.3 (senior analyst)
  - Aizen   -> Claude Sonnet 5   (Vertex AI, global) — weight 1.0
  - Gojo    -> Grok 4.5          (xAI)               — weight 1.0
  - Triage  -> Claude Haiku 4.5  (Vertex AI, global) — cheap pre-filter, no vote

v3 changes (cost + weighting):
  - CONSOLIDATED CALLS: one API call per agent per run covering ALL markets
    (JSON array in/out) instead of one call per market. ~39 calls/run -> 3-4.
  - Aizen moved from Opus 4.8 to Sonnet 5 (near-parity on this task, ~60% cheaper
    at intro pricing through Aug 31, 2026; $3/$15 after).
  - Haiku triage gate screens candidates before waking the full desk.
  - Hunt/review split: position reviews every run (6x/day); NEW-market hunts
    only on HUNT_HOURS runs (3x/day).
  - Fable weighting: Kisuke's opinion carries a 1.3x weight in the consensus
    probability, edge, sizing, and exit fair-value math. The 2-of-3 vote
    requirement is UNCHANGED — weight moves the numbers, never the vote count.

Universe: crypto, sports, climate only.

v4 change — Polymarket US migration (data + execution):
  Both the data-read layer and the order-execution layer now point at
  Polymarket US (polymarket.us, CFTC-regulated), not Polymarket International
  (polymarket.com). These are two entirely separate platforms with unrelated
  order books and prices, even for identically-worded markets. See
  SPEC_polymarket_us_migration.md (operator-provided) for the full rationale.
  Market universe changed too: US has no "weather" category (climate is the
  equivalent) and a much thinner crypto listing (~13 BTC-threshold markets vs.
  International's broader crypto board).

Cron (6x daily, UTC):
  0 0,4,8,12,16,20 * * *  cd /path/to/bot && /usr/bin/python3 orchestrator.py >> orchestrator.log 2>&1
Hunts fire automatically on the 0/8/16 UTC runs; the 4/12/20 runs are review-only.
Plus a separate watchdog cron every 20 min: orchestrator.py --watchdog-only

Env vars:
  GOOGLE_CLOUD_PROJECT, GOOGLE_APPLICATION_CREDENTIALS  (Vertex, global endpoint)
  XAI_API_KEY
  POLYMARKET_KEY_ID, POLYMARKET_SECRET_KEY  (Polymarket US; no wallet/private key)
  LIVE_TRADING ("true" for real money; default paper)
  FORCE_HUNT ("true" to force a hunt on any run, e.g. manual invocations)

Install:
  pip install "anthropic[vertex]" openai polymarket-us

NOT FINANCIAL ADVICE.
"""

import json
import os
import time
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

STARTING_BANKROLL_USD = 500.00

RISK = {
    "max_concurrent_positions": 5,
    "max_daily_loss_usd": 50.00,       # realized daily stop: halts NEW entries only
    "min_edge": 0.05,
    "min_confidence": 0.60,
    "min_liquidity_usd": 5000.00,
    "required_votes": 2,               # 2-of-3 HEADS, regardless of weights
    "kelly_fraction": 0.5,
    "position_stop_pct": 0.50,
    "trailing_arm_pct": 0.30,          # peak gain that arms the trailing take-profit
    "trailing_giveback_pct": 0.10,     # giveback from peak gain that triggers the exit
    "exit_votes_required": 2,
}

LIVE_TRADING = os.getenv("LIVE_TRADING", "false").lower() == "true"
FORCE_HUNT = os.getenv("FORCE_HUNT", "false").lower() == "true"

MARKET_TAGS = ["crypto", "sports", "climate"]   # Polymarket US categories; no "weather" — climate is the equivalent
MAX_MARKETS_PER_RUN = 8        # candidates entering triage
MAX_MARKETS_PER_CALL = 8       # anchoring guard for batched prompts
HUNT_HOURS_UTC = {0, 8, 16}    # 3 hunts/day; all 6 runs do position reviews

VERTEX_REGION = "global"
VERTEX_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "")

# weight: multiplier applied to this agent's confidence in all weighted math.
# Verify Vertex Model Garden IDs (some listings are versioned, e.g. name@YYYYMMDD).
AGENTS = [
    {"name": "Kisuke", "provider": "vertex", "model": "claude-fable-5",  "weight": 1.3, "persona": "personas/kisuke.md"},
    {"name": "Aizen",  "provider": "vertex", "model": "claude-sonnet-5", "weight": 1.0, "persona": "personas/aizen.md"},
    {"name": "Gojo",   "provider": "xai",    "model": "grok-4.5",        "weight": 1.0, "persona": "personas/gojo.md"},
]
TRIAGE_MODEL = {"provider": "vertex", "model": "claude-haiku-4-5"}

STATE_FILE = "orchestrator_state.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("orchestrator")

# ---------------------------------------------------------------------------
# DATA TYPES
# ---------------------------------------------------------------------------

@dataclass
class MarketSnapshot:
    market_id: str          # Polymarket US slug (not a conditionId — see fetch_candidate_markets)
    question: str
    description: str
    end_date: str
    # Polymarket US has no separate CLOB token ids; orders reference a market by
    # slug + intent instead of a token. Both fields carry the slug so decide_entry
    # / TradeDecision don't need a shape change downstream.
    yes_token_id: str
    no_token_id: str
    best_bid: float
    best_ask: float
    liquidity_usd: float    # proxy = volume24hr; Polymarket US exposes no liquidity figure — see fetch_candidate_markets
    volume_24h: float

@dataclass
class AgentOpinion:
    agent: str
    model: str
    weight: float
    probability_yes: float
    confidence: float
    side: str                 # "YES" | "NO" | "PASS"
    reasoning: str
    error: str = ""

@dataclass
class TradeDecision:
    market_id: str
    question: str
    side: str
    token_id: str
    limit_price: float
    size_usd: float
    shares: float
    consensus_prob: float
    edge: float
    votes: list = field(default_factory=list)

# ---------------------------------------------------------------------------
# STATE
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"cash": STARTING_BANKROLL_USD, "positions": {}, "daily": {}, "journal": []}

def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def daily_pnl(state: dict) -> float:
    return state["daily"].get(today_key(), {}).get("realized_pnl", 0.0)

def book_pnl(state: dict, amount: float) -> None:
    day = state["daily"].setdefault(today_key(), {"realized_pnl": 0.0, "trades": 0})
    day["realized_pnl"] = round(day["realized_pnl"] + amount, 2)

def journal(state: dict, kind: str, payload: dict) -> None:
    state["journal"].append({"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, **payload})

def open_slots(state: dict) -> int:
    return RISK["max_concurrent_positions"] - len(state["positions"])

# ---------------------------------------------------------------------------
# POLYMARKET US — MARKET DATA
# ---------------------------------------------------------------------------
# All facts below were verified interactively against the live API (not just
# the SDK's own doc examples, which are inconsistent with it in places):
#   - markets.list(...) returns a dict {"markets": [...]}, no pagination
#     metadata — page until a batch comes back shorter than the requested limit.
#   - Working params: limit (server caps at 500/call), closed (bool), offset
#     (confirmed to actually paginate). Non-functional/silently-ignored params:
#     active, category (as a filter arg), sort, order, page — filter client-side.
#   - retrieveBySlug (camelCase, per SDK quickstart docs) does not exist on the
#     client; the real method is snake_case: markets.retrieve_by_slug(slug).
#   - No liquidityNum/liquidity/clobTokenIds shape exists on these market
#     objects at all (confirmed empty across the full ~5,500-market open
#     universe) — see the liquidity_proxy note below.
#   - `outcomes` (the plain-language label array, e.g. ["Titans","Chargers"])
#     does NOT line up positionally with `marketSides`' own long/short flag —
#     a resolved 2-team market had outcomes=["Titans","Chargers"] but the
#     long/winning marketSide was "Chargers", listed *second*. Reading
#     outcomes[0]/outcomePrices[0] as "the YES side" the way Gamma's fixed
#     ["Yes","No"] convention allowed is therefore unsafe here. (outcomePrices
#     itself happened to read [1.0, 0.0] = [long, short] in that same example —
#     i.e. it may already be long-first regardless of `outcomes`' order — but
#     that's one data point, not a documented guarantee, so fetch_market_status
#     below derives outcome_prices directly from marketSides' `long` flag
#     instead of trusting positional array order at all.)

_pmus_client = None

def pmus_client():
    global _pmus_client
    if _pmus_client is None:
        from polymarket_us import PolymarketUS
        _pmus_client = PolymarketUS(
            key_id=os.environ["POLYMARKET_KEY_ID"],
            secret_key=os.environ["POLYMARKET_SECRET_KEY"],
        )
    return _pmus_client

def fetch_candidate_markets(exclude_ids: set) -> list:
    from collections import Counter
    raw_markets, offset, page_size = [], 0, 500
    terminated_naturally = False
    hit_safety_cap = False
    
    while True:
        try:
            resp = pmus_client().markets.list({"limit": page_size, "closed": False, "offset": offset})
        except Exception as e:
            log.warning(f"markets.list failed at offset {offset}: {e}")
            break
        batch = resp.get("markets") or []
        raw_markets.extend(batch)
        
        log.info(f"[DEBUG] Fetching pagination loop: offset={offset}, fetched_batch_size={len(batch)}, running_total={len(raw_markets)}")
        
        if len(batch) < page_size:
            terminated_naturally = True
            log.info(f"[DEBUG] Pagination terminating naturally: batch size {len(batch)} is less than page size {page_size} at offset {offset}.")
            break
        offset += page_size
        if offset > 10000:      # safety cap; the full open-market universe is ~5.5k
            hit_safety_cap = True
            log.warning(f"[DEBUG] Pagination hit safety cap at offset {offset} (limit 10000).")
            break

    log.info(f"[DEBUG] Pagination completed. terminated_naturally={terminated_naturally}, hit_safety_cap={hit_safety_cap}")
    log.info(f"[DEBUG] Total open-market count fetched: {len(raw_markets)}")
    
    # Print a Counter of the category field across all fetched markets before the MARKET_TAGS filter is applied
    category_counts = Counter(m.get("category") for m in raw_markets)
    log.info(f"[DEBUG] Category field Counter across all fetched markets before filter: {dict(category_counts)}")
    
    # Confirming filter condition targets category field and not tags field
    log.info("[DEBUG] Filtering candidates by checking m.get('category') in MARKET_TAGS. Note that m.get('tags') is not used here.")
    candidates = [m for m in raw_markets if m.get("category") in MARKET_TAGS]
    log.info(f"[DEBUG] Filtered from {len(raw_markets)} to {len(candidates)} candidates matching category in {MARKET_TAGS}")
    cheap_candidates = []
    for m in candidates:
        try:
            if m.get("closed") or not m.get("active", True):
                continue
            mid = m.get("slug", "")
            if not mid or mid in exclude_ids:
                continue

            # Extract pricing from list() object fields bestBidQuote and bestAskQuote
            bid_quote = m.get("bestBidQuote")
            ask_quote = m.get("bestAskQuote")
            if not bid_quote or not ask_quote:
                continue
            try:
                bid_price = float(bid_quote.get("value") or 0)
                ask_price = float(ask_quote.get("value") or 0)
            except (TypeError, ValueError):
                continue

            # Exclude prices outside ~0.03–0.97
            if bid_price < 0.03 or bid_price > 0.97 or ask_price < 0.03 or ask_price > 0.97:
                continue

            cheap_candidates.append((m, bid_price, ask_price))
        except Exception as e:
            log.debug(f"skip candidate in cheap pass: {e}")

    # Sort candidates by soonest endDate/gameStartTime
    def get_soonest_time(item):
        m = item[0]
        t1 = m.get("gameStartTime")
        t2 = m.get("endDate")
        times = [t for t in (t1, t2) if t]
        return min(times) if times else "9999-12-31T23:59:59Z"

    cheap_candidates.sort(key=get_soonest_time)
    
    # Shortlist top 100
    shortlist = cheap_candidates[:100]
    
    snapshots = []
    book_checked = 0
    book_cleared = 0

    for m, bid_price, ask_price in shortlist:
        mid = m.get("slug")
        try:
            book_checked += 1
            book_resp = pmus_client().markets.book(mid)
            market_data = book_resp.get("marketData") or {}
            bids = market_data.get("bids") or []
            offers = market_data.get("offers") or []
            if not bids or not offers:
                continue

            # Compute liquidity from top book level
            top_bid = bids[0]
            top_offer = offers[0]

            top_bid_px = float(top_bid["px"]["value"])
            top_bid_qty = float(top_bid["qty"])
            top_ask_px = float(top_offer["px"]["value"])
            top_ask_qty = float(top_offer["qty"])

            liquidity_usd = (top_bid_px * top_bid_qty) + (top_ask_px * top_ask_qty)

            if liquidity_usd < RISK["min_liquidity_usd"]:
                continue

            book_cleared += 1

            snapshots.append(MarketSnapshot(
                market_id=mid, question=m.get("question", ""),
                description=(m.get("description") or "")[:1200],
                end_date=m.get("endDate", ""),
                yes_token_id=mid, no_token_id=mid,
                best_bid=bid_price, best_ask=ask_price,
                liquidity_usd=liquidity_usd, volume_24h=liquidity_usd,
            ))
        except Exception as e:
            log.debug(f"skip market: {e}")
        if len(snapshots) >= MAX_MARKETS_PER_RUN:
            break

    log.info(f"Book liquidity check: checked {book_checked} candidates via book(), {book_cleared} cleared the min_liquidity_usd threshold of {RISK['min_liquidity_usd']}.")
    log.info(f"{len(snapshots)} candidates [{', '.join(MARKET_TAGS)}]")
    return snapshots

def fetch_top_of_book(slug: str):
    try:
        data = pmus_client().markets.bbo(slug)["marketData"]
        bid, ask = data.get("bestBid"), data.get("bestAsk")
        if not bid or not ask:
            return None
        return {"bid": float(bid["value"]), "ask": float(ask["value"])}
    except Exception:
        return None

def fetch_market_status(market_id: str):
    try:
        m = pmus_client().markets.retrieve_by_slug(market_id)["market"]
        sides = {s.get("long"): s for s in (m.get("marketSides") or [])}
        if True not in sides or False not in sides:
            return None
        # Build outcome_prices ourselves as [long_price, short_price] using the
        # `long` flag directly, rather than trusting outcomes/outcomePrices
        # array order (see module-level note above). This keeps settle_resolved()
        # correct without needing any change there: index 0 is guaranteed to be
        # the long/"YES" payout, verified end-to-end against a real resolved
        # market (Chargers/long won at $1, correctly returned as index 0 here
        # even though "Chargers" is outcomes[1], not outcomes[0]).
        outcome_prices = [float(sides[True]["price"]), float(sides[False]["price"])]
        return {"closed": bool(m.get("closed")), "outcome_prices": outcome_prices}
    except Exception:
        return None

# ---------------------------------------------------------------------------
# MODEL CALLS (Vertex global + xAI)
# ---------------------------------------------------------------------------

_vertex_client = None

def vertex_client():
    global _vertex_client
    if _vertex_client is None:
        from anthropic import AnthropicVertex
        if not VERTEX_PROJECT:
            raise RuntimeError("GOOGLE_CLOUD_PROJECT is not set")
        _vertex_client = AnthropicVertex(project_id=VERTEX_PROJECT, region=VERTEX_REGION)
    return _vertex_client

def call_model(provider: str, model: str, prompt: str, max_tokens: int) -> str:
    if provider == "vertex":
        resp = vertex_client().messages.create(
            model=model, max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    if provider == "xai":
        from openai import OpenAI
        client = OpenAI(api_key=os.environ["XAI_API_KEY"], base_url="https://api.x.ai/v1")
        resp = client.chat.completions.create(
            model=model, max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}])
        return resp.choices[0].message.content or ""
    raise ValueError(f"Unknown provider {provider}")

def strip_fences(raw: str) -> str:
    return raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()

def parse_json_lenient(raw: str):
    """json.loads with a fallback that strips trailing commas (some models emit
    `"reasoning": "...", }` which strict JSON rejects)."""
    cleaned = strip_fences(raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        import re
        return json.loads(re.sub(r",\s*([}\]])", r"\1", cleaned))

_persona_cache: dict = {}

def load_persona(agent: dict) -> str:
    """Load the agent's persona/operating-parameter file (cached). Missing file = no persona."""
    path = agent.get("persona", "")
    if not path:
        return ""
    if path not in _persona_cache:
        try:
            with open(path) as f:
                _persona_cache[path] = f.read().strip()
        except FileNotFoundError:
            log.warning(f"Persona file not found for {agent['name']}: {path} — running without it.")
            _persona_cache[path] = ""
    return _persona_cache[path]

# ---------------------------------------------------------------------------
# TRIAGE (Haiku): cheap screen before waking the full desk
# ---------------------------------------------------------------------------

TRIAGE_PROMPT = """You are a triage analyst screening prediction markets for a trading desk.
For each market below, decide if it is WORTH a full analysis: a market is worth it if
a well-informed analyst could plausibly disagree with the current price by 5+ cents.
Skip markets that are efficiently priced, pure coin-flips, or depend on unknowable info.

MARKETS:
{markets_block}

Return ONLY a JSON array of the integer indices worth full analysis, e.g. [0,2,5].
No markdown fences, no commentary. An empty array [] is a valid answer."""

def triage(markets: list) -> list:
    if not markets:
        return []
    block = "\n".join(
        f"[{i}] {m.question} | YES bid {m.best_bid:.2f} / ask {m.best_ask:.2f} | ends {m.end_date}"
        for i, m in enumerate(markets))
    try:
        raw = call_model(TRIAGE_MODEL["provider"], TRIAGE_MODEL["model"],
                         TRIAGE_PROMPT.format(markets_block=block), max_tokens=200)
        keep = [i for i in parse_json_lenient(raw) if isinstance(i, int) and 0 <= i < len(markets)]
        log.info(f"Triage kept {len(keep)}/{len(markets)}: {keep}")
        return [markets[i] for i in keep]
    except Exception as e:
        log.warning(f"Triage failed ({e}) — passing all candidates through.")
        return markets  # fail open: triage is a cost optimization, not a gate on safety

# ---------------------------------------------------------------------------
# BATCHED DESK CONSULTATION (one call per agent covering all markets)
# ---------------------------------------------------------------------------

ENTRY_BATCH_PROMPT = """You are {name}, an analyst on a three-agent prediction-market desk.
Independently analyze EACH market below. Return ONLY a JSON array — no fences, no preamble.
Analyze each market on its own merits; do not let one market's analysis influence another.

For each market provide:
- market_index: the integer index shown in brackets
- probability_yes: your independent estimate of the TRUE probability of YES (0.0-1.0).
  Estimate FIRST, then compare to the market price. Do not anchor on the price.
- confidence: honesty about your uncertainty (0.0-1.0)
- side: "YES" if YES is underpriced, "NO" if NO is underpriced, "PASS" if no verifiable edge
- reasoning: max 2 sentences

MARKETS:
{markets_block}

Return exactly one array element per market, in this shape:
[{{"market_index": 0, "probability_yes": 0.0, "confidence": 0.0, "side": "YES", "reasoning": ""}}, ...]"""

REVIEW_BATCH_PROMPT = """You are {name}, reviewing OPEN POSITIONS for a three-agent prediction-market desk.
Re-underwrite EACH position from scratch with fresh eyes. Return ONLY a JSON array — no fences.

For each position provide:
- market_index: the integer index shown in brackets
- probability_yes: your CURRENT estimate of the true probability of YES (0.0-1.0)
- confidence: 0.0-1.0
- side: the side you would take TODAY: "YES", "NO", or "PASS"
- reasoning: max 2 sentences

POSITIONS:
{markets_block}

Return exactly one array element per position:
[{{"market_index": 0, "probability_yes": 0.0, "confidence": 0.0, "side": "YES", "reasoning": ""}}, ...]"""

def _pass_opinion(agent: dict, err: str = "") -> AgentOpinion:
    return AgentOpinion(agent=agent["name"], model=agent["model"], weight=agent["weight"],
                        probability_yes=0.5, confidence=0.0, side="PASS",
                        reasoning="", error=err[:200])

def consult_batch(prompt_template: str, markets_block: str, n_items: int) -> dict:
    """Returns {agent_name: [AgentOpinion per item index]}. Malformed item -> forced PASS."""
    desk = {}
    for agent in AGENTS:
        opinions = [_pass_opinion(agent, "missing") for _ in range(n_items)]
        try:
            prompt = prompt_template.format(name=agent["name"], markets_block=markets_block)
            persona = load_persona(agent)
            if persona:
                prompt = persona + "\n\n---\n\n" + prompt
            # Budget must cover thinking tokens too (Sonnet 5 on Vertex thinks
            # by default and thinking counts against max_tokens).
            raw = call_model(agent["provider"], agent["model"], prompt,
                             max_tokens=500 * n_items + 2000)
            for item in parse_json_lenient(raw):
                try:
                    idx = int(item["market_index"])
                    if not 0 <= idx < n_items:
                        continue
                    side = str(item.get("side", "PASS")).upper()
                    opinions[idx] = AgentOpinion(
                        agent=agent["name"], model=agent["model"], weight=agent["weight"],
                        probability_yes=max(0.0, min(1.0, float(item["probability_yes"]))),
                        confidence=max(0.0, min(1.0, float(item["confidence"]))),
                        side=side if side in ("YES", "NO", "PASS") else "PASS",
                        reasoning=str(item.get("reasoning", ""))[:400])
                except Exception:
                    continue  # this item stays a forced PASS
        except Exception as e:
            log.warning(f"{agent['name']} batch failed: {e}")
            opinions = [_pass_opinion(agent, str(e)) for _ in range(n_items)]
        desk[agent["name"]] = opinions
    return desk

def opinions_for(desk: dict, idx: int) -> list:
    return [desk[a["name"]][idx] for a in AGENTS]

def weighted_consensus_yes(opinions: list):
    """Confidence x agent-weight consensus. Kisuke (Fable) carries 1.3x."""
    active = [o for o in opinions if o.confidence > 0]
    if not active:
        return None
    w = sum(o.confidence * o.weight for o in active)
    return sum(o.probability_yes * o.confidence * o.weight for o in active) / w

# ---------------------------------------------------------------------------
# ENTRY DECISION ENGINE (deterministic; weights move numbers, never votes)
# ---------------------------------------------------------------------------

def decide_entry(mkt: MarketSnapshot, opinions: list, state: dict):
    if daily_pnl(state) <= -RISK["max_daily_loss_usd"]:
        return None
    slots = open_slots(state)
    if slots <= 0 or mkt.market_id in state["positions"]:
        return None

    for target_side in ("YES", "NO"):
        voters = [o for o in opinions
                  if o.side == target_side and o.confidence >= RISK["min_confidence"]]
        # HEAD COUNT: 2 of 3 agents must agree. Weight plays no role here.
        if len(voters) < RISK["required_votes"]:
            continue

        # WEIGHTED consensus among the voters (Fable counts 1.3x)
        w = sum(v.confidence * v.weight for v in voters)
        consensus_yes = sum(v.probability_yes * v.confidence * v.weight for v in voters) / w

        if target_side == "YES":
            entry_price, token_id = mkt.best_ask, mkt.yes_token_id
            edge = consensus_yes - entry_price
        else:
            entry_price, token_id = round(1.0 - mkt.best_bid, 2), mkt.no_token_id
            edge = (1.0 - consensus_yes) - entry_price

        if edge < RISK["min_edge"] or not (0.02 < entry_price < 0.98):
            continue

        kelly_size = state["cash"] * edge * RISK["kelly_fraction"]
        slot_cap = state["cash"] / max(slots, 1)
        size = round(min(max(kelly_size, 0), slot_cap, state["cash"]), 2)
        if size < 5.00:
            continue

        return TradeDecision(
            market_id=mkt.market_id, question=mkt.question, side=target_side,
            token_id=token_id, limit_price=round(entry_price, 2),
            size_usd=size, shares=round(size / entry_price, 2),
            consensus_prob=round(consensus_yes, 3), edge=round(edge, 3),
            votes=[asdict(v) for v in voters])
    return None

# ---------------------------------------------------------------------------
# EXECUTION (Polymarket US — no wallet, no private key, no on-chain gas token)
# ---------------------------------------------------------------------------
# Verified against the SDK's own type stubs (typing.get_type_hints), which are
# generated from the real API contract and more reliable here than the SDK's
# prose docs:
#   - CreateOrderParams field is `marketSlug` (confirmed, not bare `slug`).
#   - intent is one of ORDER_INTENT_{BUY,SELL}_{LONG,SHORT}. marketSides carry
#     an explicit `long` bool, and our desk's "YES" = the long side, "NO" = the
#     short side (bestBidQuote/bestAskQuote on the market object are quoted for
#     the long side, matching decide_entry's existing YES pricing).
#   - orders.close_position({"marketSlug": slug}) exits whatever side the
#     account actually holds — no need to track/guess a SELL_LONG vs SELL_SHORT
#     intent for an exit, which removes a whole class of bug. Used for every
#     exit path (hard stop, trailing take-profit, consensus flip, edge exhausted).
#   - quantity is typed int; minimumTradeQty was 1 on every market checked, so
#     whole-share sizing is required (no fractional shares like International).
#   - Order/close responses are plain dicts with an "executions" list; each
#     execution has a `type` (…_FILL / …_PARTIAL_FILL / …_NEW / etc.) and a
#     `lastPx` fill price that can differ from the requested limit price.
#   - No `redeem` method exists anywhere in the SDK. portfolio/account
#     endpoints expose an ACTIVITY_TYPE_POSITION_RESOLUTION activity type,
#     confirming settlement of resolved positions is automatic (cash-settled,
#     not on-chain CTF tokens) — settle_resolved() below needs no extra API call.

INTENT_FOR_SIDE = {"YES": "ORDER_INTENT_BUY_LONG", "NO": "ORDER_INTENT_BUY_SHORT"}

def place_entry_order(slug: str, side: str, price: float, shares: float) -> dict:
    return pmus_client().orders.create({
        "marketSlug": slug,
        "intent": INTENT_FOR_SIDE[side],
        "type": "ORDER_TYPE_LIMIT",
        "price": {"value": f"{price:.3f}", "currency": "USD"},
        "quantity": max(1, int(shares)),
        "tif": "TIME_IN_FORCE_GOOD_TILL_CANCEL",
    })

def close_position_order(slug: str) -> dict:
    return pmus_client().orders.close_position({"marketSlug": slug})

def extract_fill(resp: dict):
    """(fill_price, fill_shares) from the last FILL/PARTIAL_FILL execution in an
    order/close-position response, or (None, None) if nothing has filled yet
    (e.g. a GTC limit order still resting on the book)."""
    for ex in reversed((resp or {}).get("executions") or []):
        if ex.get("type") in ("EXECUTION_TYPE_FILL", "EXECUTION_TYPE_PARTIAL_FILL") and ex.get("lastPx"):
            try:
                return float(ex["lastPx"]["value"]), float(ex.get("lastShares") or 0)
            except Exception:
                continue
    return None, None

def execute_entry(d: TradeDecision, mkt: MarketSnapshot, state: dict) -> None:
    mode = "LIVE" if LIVE_TRADING else "PAPER"
    resp, fill_price, shares, cost = None, d.limit_price, d.shares, d.size_usd
    if LIVE_TRADING:
        resp = place_entry_order(d.token_id, d.side, d.limit_price, d.shares)
        fp, fq = extract_fill(resp)
        if fp is not None:
            fill_price = fp
        # Use the ACTUAL filled quantity, not a value back-derived from the
        # requested size_usd: the order sent quantity=int(d.shares) (already
        # truncated), and a fill price that differs from the limit price would
        # otherwise silently desync recorded shares/cash from what was really
        # bought. Falls back to the submitted (int-truncated) quantity only if
        # nothing has filled yet (e.g. a GTC order still resting on the book).
        shares = fq if fq else float(max(1, int(d.shares)))
        cost = round(shares * fill_price, 2)
    log.info(f"[{mode}] BUY {d.side} {shares} sh @ {fill_price:.2f} (${cost:.2f}) :: {d.question[:60]}")
    state["cash"] = round(state["cash"] - cost, 2)
    state["positions"][d.market_id] = {
        "question": d.question, "side": d.side, "token_id": d.token_id,
        "entry_price": fill_price, "size_usd": cost, "shares": shares,
        "peak_price": fill_price,
        "consensus_at_entry": d.consensus_prob, "end_date": mkt.end_date,
        "paper": not LIVE_TRADING, "opened_at": datetime.now(timezone.utc).isoformat()}
    day = state["daily"].setdefault(today_key(), {"realized_pnl": 0.0, "trades": 0})
    day["trades"] += 1
    journal(state, "entry", {"decision": asdict(d), "live": LIVE_TRADING,
                             "requested_price": d.limit_price, "fill_price": fill_price,
                             "requested_size_usd": d.size_usd, "actual_cost_usd": cost,
                             "order_response": resp})
    save_state(state)

def execute_exit(market_id: str, pos: dict, exit_price: float, reason: str, state: dict) -> None:
    mode = "LIVE" if LIVE_TRADING else "PAPER"
    resp, fill_price, closed_shares = None, exit_price, pos["shares"]
    if LIVE_TRADING and not pos.get("paper"):
        resp = close_position_order(market_id)
        fp, fq = extract_fill(resp)
        if fp is not None:
            fill_price = fp
        if fq:
            closed_shares = fq
            if round(closed_shares, 2) < round(pos["shares"], 2):
                # We still record the position as fully closed below (this code
                # has no partial-position model) — flagging loudly rather than
                # silently mis-accounting the unfilled remainder.
                log.warning(f"close_position only partially filled ({closed_shares}/{pos['shares']} sh) "
                            f"for {pos['question'][:50]!r} — treating as fully closed; "
                            f"P&L will be understated for the unfilled remainder.")
    proceeds = round(closed_shares * fill_price, 2)
    pnl = round(proceeds - pos["size_usd"], 2)
    state["cash"] = round(state["cash"] + proceeds, 2)
    book_pnl(state, pnl)
    log.info(f"[{mode}] EXIT ({reason}) {pos['side']} @ {fill_price:.2f} | P&L ${pnl:+.2f} :: {pos['question'][:60]}")
    journal(state, "exit", {"market_id": market_id, "reason": reason,
                            "requested_exit_price": exit_price, "fill_price": fill_price,
                            "closed_shares": closed_shares, "pnl": pnl, "order_response": resp})
    del state["positions"][market_id]
    save_state(state)

def settle_resolved(market_id: str, pos: dict, outcome_prices, state: dict) -> None:
    try:
        yes_payout = float(outcome_prices[0])
    except Exception:
        yes_payout = 0.0
    per_share = yes_payout if pos["side"] == "YES" else (1.0 - yes_payout)
    proceeds = round(pos["shares"] * per_share, 2)
    pnl = round(proceeds - pos["size_usd"], 2)
    state["cash"] = round(state["cash"] + proceeds, 2)
    book_pnl(state, pnl)
    log.info(f"[SETTLED] {pos['side']} paid {per_share:.2f}/sh | P&L ${pnl:+.2f} :: {pos['question'][:60]}")
    journal(state, "settlement", {"market_id": market_id, "pnl": pnl, "payout_per_share": per_share})
    del state["positions"][market_id]
    save_state(state)

# ---------------------------------------------------------------------------
# POSITION MONITORING / EXIT ENGINE (runs every cycle, batched desk review)
# ---------------------------------------------------------------------------

def watchdog_pass(state: dict) -> list:
    """Pass 1: settlements, peak-price update, hard stop, trailing take-profit.
    No model calls. Returns positions still held as [(market_id, pos, mark_bid)]."""
    reviewable = []
    for market_id in list(state["positions"].keys()):
        pos = state["positions"][market_id]
        status = fetch_market_status(market_id)
        if status and status["closed"]:
            settle_resolved(market_id, pos, status.get("outcome_prices") or [], state)
            continue
        book = fetch_top_of_book(pos["token_id"])
        if book is None:
            log.warning(f"No book for {pos['question'][:50]} — holding.")
            continue
        mark = book["bid"]
        pos["peak_price"] = max(pos.get("peak_price", pos["entry_price"]), mark)
        if mark <= pos["entry_price"] * (1 - RISK["position_stop_pct"]):
            execute_exit(market_id, pos, mark, "hard_stop", state)
            continue
        gain_now = mark / pos["entry_price"] - 1
        peak_gain = pos["peak_price"] / pos["entry_price"] - 1
        if (peak_gain >= RISK["trailing_arm_pct"]
                and gain_now <= peak_gain - RISK["trailing_giveback_pct"]):
            execute_exit(market_id, pos, mark, "trailing_take_profit", state)
            continue
        reviewable.append((market_id, pos, mark))
    return reviewable

def manage_positions(state: dict) -> None:
    if not state["positions"]:
        return
    log.info(f"--- Managing {len(state['positions'])} open position(s) ---")

    reviewable = watchdog_pass(state)
    if not reviewable:
        return

    # Pass 2: one batched review call per agent for everything still held
    block = "\n".join(
        f"[{i}] {pos['question']} | our side: {pos['side']} entered {pos['entry_price']:.2f} "
        f"| current sell price {mark:.2f} | ends {pos.get('end_date', 'unknown')}"
        for i, (mid, pos, mark) in enumerate(reviewable))
    desk = consult_batch(REVIEW_BATCH_PROMPT, block, len(reviewable))
    journal(state, "review", {"positions": [mid for mid, _, _ in reviewable],
                              "desk": {k: [asdict(o) for o in v] for k, v in desk.items()}})

    for i, (market_id, pos, mark) in enumerate(reviewable):
        opinions = opinions_for(desk, i)
        for o in opinions:
            log.info(f"  {o.agent} on '{pos['question'][:40]}': {o.side} p={o.probability_yes:.2f} c={o.confidence:.2f}")
        opposite = "NO" if pos["side"] == "YES" else "YES"
        flippers = [o for o in opinions
                    if o.side == opposite and o.confidence >= RISK["min_confidence"]]
        supporters = [o for o in opinions if o.side == pos["side"]]

        if len(flippers) >= RISK["exit_votes_required"]:           # head count, unweighted
            execute_exit(market_id, pos, mark, "consensus_flip", state)
            continue

        consensus_yes = weighted_consensus_yes(opinions)           # Fable-weighted fair value
        if consensus_yes is not None:
            fair = consensus_yes if pos["side"] == "YES" else 1 - consensus_yes
            if mark >= fair and not supporters:
                execute_exit(market_id, pos, mark, "edge_exhausted", state)
                continue
        log.info(f"  -> HOLD ({len(supporters)} supporting)")

# ---------------------------------------------------------------------------
# HUNT (only on HUNT_HOURS runs): triage -> batched desk -> entries
# ---------------------------------------------------------------------------

def hunt(state: dict) -> None:
    if open_slots(state) <= 0:
        log.info("All slots full — skipping hunt.")
        return
    if daily_pnl(state) <= -RISK["max_daily_loss_usd"]:
        log.info("Daily stop hit — skipping hunt (exits remain active).")
        return

    markets = fetch_candidate_markets(exclude_ids=set(state["positions"].keys()))
    markets = triage(markets)[:MAX_MARKETS_PER_CALL]
    if not markets:
        log.info("Nothing survived triage.")
        return

    block = "\n".join(
        f"[{i}] {m.question} | RESOLUTION: {m.description} | ends {m.end_date} "
        f"| YES bid {m.best_bid:.2f} / ask {m.best_ask:.2f} | 24h vol ${m.volume_24h:,.0f}"
        for i, m in enumerate(markets))
    desk = consult_batch(ENTRY_BATCH_PROMPT, block, len(markets))
    journal(state, "hunt", {
        "candidates": [m.question for m in markets],
        "desk": {k: [asdict(o) for o in v] for k, v in desk.items()},
    })
    save_state(state)

    for i, mkt in enumerate(markets):
        if open_slots(state) <= 0:
            break
        decision = decide_entry(mkt, opinions_for(desk, i), state)
        if decision:
            execute_entry(decision, mkt, state)
            time.sleep(2)

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def run_once() -> None:
    state = load_state()
    hour = datetime.now(timezone.utc).hour
    hunting = FORCE_HUNT or hour in HUNT_HOURS_UTC
    mode = "LIVE" if LIVE_TRADING else "PAPER"
    log.info(f"=== Run | mode={mode} | {'HUNT+REVIEW' if hunting else 'REVIEW-ONLY'} "
             f"| cash=${state['cash']:.2f} | positions={len(state['positions'])} "
             f"| daily P&L=${daily_pnl(state):.2f} ===")

    manage_positions(state)     # every run: settlements, stops, batched reviews
    if hunting:
        hunt(state)             # 3x/day: triage + batched entry analysis

    save_state(state)
    log.info(f"Run complete | cash=${state['cash']:.2f} | positions={len(state['positions'])}")

def run_watchdog_only() -> None:
    """Mechanical safety pass between full desk runs: settlements, hard stops,
    trailing take-profits. No agent calls, no hunting."""
    state = load_state()
    n_before = len(state["positions"])
    held = watchdog_pass(state) if state["positions"] else []
    save_state(state)
    log.info(f"Watchdog | positions {n_before} -> {len(state['positions'])} "
             f"({len(held)} held) | cash=${state['cash']:.2f} "
             f"| daily P&L=${daily_pnl(state):.2f}")

if __name__ == "__main__":
    import sys
    if "--watchdog-only" in sys.argv[1:]:
        run_watchdog_only()
    else:
        run_once()
