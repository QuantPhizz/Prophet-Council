"""
Multi-Agent Prediction Market Orchestrator — v3
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

Universe: crypto, sports, weather only.

Cron (6x daily, UTC):
  0 0,4,8,12,16,20 * * *  cd /path/to/bot && /usr/bin/python3 orchestrator.py >> orchestrator.log 2>&1
Hunts fire automatically on the 0/8/16 UTC runs; the 4/12/20 runs are review-only.

Env vars:
  GOOGLE_CLOUD_PROJECT, GOOGLE_APPLICATION_CREDENTIALS  (Vertex, global endpoint)
  XAI_API_KEY
  LIVE_TRADING ("true" for real money; default paper)
  POLY_PRIVATE_KEY (live only)
  FORCE_HUNT ("true" to force a hunt on any run, e.g. manual invocations)

Install:
  pip install "anthropic[vertex]" openai py-clob-client requests

NOT FINANCIAL ADVICE.
"""

import json
import os
import time
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

import requests

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
    "exit_votes_required": 2,
}

LIVE_TRADING = os.getenv("LIVE_TRADING", "false").lower() == "true"
FORCE_HUNT = os.getenv("FORCE_HUNT", "false").lower() == "true"

MARKET_TAGS = ["crypto", "sports", "weather"]
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

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
STATE_FILE = "orchestrator_state.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("orchestrator")

# ---------------------------------------------------------------------------
# DATA TYPES
# ---------------------------------------------------------------------------

@dataclass
class MarketSnapshot:
    market_id: str
    question: str
    description: str
    end_date: str
    yes_token_id: str
    no_token_id: str
    best_bid: float
    best_ask: float
    liquidity_usd: float
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
# POLYMARKET MARKET DATA
# ---------------------------------------------------------------------------

def fetch_candidate_markets(exclude_ids: set) -> list:
    # Gamma's /markets listing returns tags=null; tags are only filterable on
    # /events via tag_slug. Pull tagged events, then flatten their markets.
    raw_markets, seen = [], set()
    for tag in MARKET_TAGS:
        try:
            r = requests.get(f"{GAMMA_API}/events",
                             params={"active": "true", "closed": "false", "limit": 50,
                                     "order": "volume24hr", "ascending": "false",
                                     "tag_slug": tag}, timeout=15)
            r.raise_for_status()
            for ev in r.json():
                for m in ev.get("markets") or []:
                    key = m.get("conditionId", m.get("id", ""))
                    if key and key not in seen:
                        seen.add(key)
                        raw_markets.append(m)
        except Exception as e:
            log.warning(f"event fetch failed for tag '{tag}': {e}")
    raw_markets.sort(key=lambda m: float(m.get("volume24hr") or 0), reverse=True)

    snapshots = []
    for m in raw_markets:
        try:
            if m.get("closed") or not m.get("active", True):
                continue
            mid = m.get("conditionId", m.get("id", ""))
            if mid in exclude_ids:
                continue
            liquidity = float(m.get("liquidityNum") or m.get("liquidity") or 0)
            if liquidity < RISK["min_liquidity_usd"]:
                continue
            token_ids = m.get("clobTokenIds")
            if isinstance(token_ids, str):
                token_ids = json.loads(token_ids)
            if not token_ids or len(token_ids) != 2:
                continue
            book = fetch_top_of_book(token_ids[0])
            if book is None:
                continue
            snapshots.append(MarketSnapshot(
                market_id=mid, question=m.get("question", ""),
                description=(m.get("description") or "")[:1200],
                end_date=m.get("endDate", ""),
                yes_token_id=token_ids[0], no_token_id=token_ids[1],
                best_bid=book["bid"], best_ask=book["ask"],
                liquidity_usd=liquidity, volume_24h=float(m.get("volume24hr") or 0),
            ))
        except Exception as e:
            log.debug(f"skip market: {e}")
        if len(snapshots) >= MAX_MARKETS_PER_RUN:
            break
    log.info(f"{len(snapshots)} candidates [{', '.join(MARKET_TAGS)}]")
    return snapshots

def fetch_top_of_book(token_id: str):
    try:
        r = requests.get(f"{CLOB_API}/book", params={"token_id": token_id}, timeout=10)
        r.raise_for_status()
        book = r.json()
        bids, asks = book.get("bids") or [], book.get("asks") or []
        if not bids or not asks:
            return None
        return {"bid": float(bids[-1]["price"]), "ask": float(asks[-1]["price"])}
    except Exception:
        return None

def fetch_market_status(market_id: str):
    try:
        r = requests.get(f"{GAMMA_API}/markets", params={"condition_ids": market_id}, timeout=15)
        r.raise_for_status()
        data = r.json()
        if not data:
            return None
        m = data[0]
        prices = m.get("outcomePrices")
        if isinstance(prices, str):
            prices = json.loads(prices)
        return {"closed": bool(m.get("closed")), "outcome_prices": prices}
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
# EXECUTION
# ---------------------------------------------------------------------------

def clob_client():
    from py_clob_client.client import ClobClient
    client = ClobClient(CLOB_API, key=os.environ["POLY_PRIVATE_KEY"], chain_id=137)
    client.set_api_creds(client.create_or_derive_api_creds())
    return client

def place_order(token_id: str, price: float, shares: float, side: str):
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY, SELL
    client = clob_client()
    order = OrderArgs(price=price, size=shares,
                      side=BUY if side == "BUY" else SELL, token_id=token_id)
    return client.post_order(client.create_order(order), OrderType.GTC)

def execute_entry(d: TradeDecision, mkt: MarketSnapshot, state: dict) -> None:
    mode = "LIVE" if LIVE_TRADING else "PAPER"
    resp = place_order(d.token_id, d.limit_price, d.shares, "BUY") if LIVE_TRADING else None
    log.info(f"[{mode}] BUY {d.side} {d.shares} sh @ {d.limit_price:.2f} (${d.size_usd:.2f}) :: {d.question[:60]}")
    state["cash"] = round(state["cash"] - d.size_usd, 2)
    state["positions"][d.market_id] = {
        "question": d.question, "side": d.side, "token_id": d.token_id,
        "entry_price": d.limit_price, "size_usd": d.size_usd, "shares": d.shares,
        "consensus_at_entry": d.consensus_prob, "end_date": mkt.end_date,
        "paper": not LIVE_TRADING, "opened_at": datetime.now(timezone.utc).isoformat()}
    day = state["daily"].setdefault(today_key(), {"realized_pnl": 0.0, "trades": 0})
    day["trades"] += 1
    journal(state, "entry", {"decision": asdict(d), "live": LIVE_TRADING,
                             "order_response": str(resp) if resp else None})
    save_state(state)

def execute_exit(market_id: str, pos: dict, exit_price: float, reason: str, state: dict) -> None:
    mode = "LIVE" if LIVE_TRADING else "PAPER"
    resp = None
    if LIVE_TRADING and not pos.get("paper"):
        resp = place_order(pos["token_id"], round(exit_price, 2), pos["shares"], "SELL")
    proceeds = round(pos["shares"] * exit_price, 2)
    pnl = round(proceeds - pos["size_usd"], 2)
    state["cash"] = round(state["cash"] + proceeds, 2)
    book_pnl(state, pnl)
    log.info(f"[{mode}] EXIT ({reason}) {pos['side']} @ {exit_price:.2f} | P&L ${pnl:+.2f} :: {pos['question'][:60]}")
    journal(state, "exit", {"market_id": market_id, "reason": reason,
                            "exit_price": exit_price, "pnl": pnl,
                            "order_response": str(resp) if resp else None})
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

def manage_positions(state: dict) -> None:
    if not state["positions"]:
        return
    log.info(f"--- Managing {len(state['positions'])} open position(s) ---")

    # Pass 1: settlements + hard stops (no model calls needed)
    reviewable = []   # (market_id, pos, mark_bid)
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
        if mark <= pos["entry_price"] * (1 - RISK["position_stop_pct"]):
            execute_exit(market_id, pos, mark, "hard_stop", state)
            continue
        reviewable.append((market_id, pos, mark))

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

if __name__ == "__main__":
    run_once()
