# Prophet Council

Multi-agent trading desk for **Polymarket US** (polymarket.us, CFTC-regulated).
Three LLM analyst personas (Kisuke, Aizen, Gojo) plus a cheap triage pre-filter
analyze crypto / sports / climate prediction markets and paper-trade a shared
bankroll. Not financial advice.

> Migrated from Polymarket International (polymarket.com) on 2026-07-11 — see
> `SPEC_polymarket_us_migration.md`. International and US are separate
> platforms with unrelated order books; nothing from the old International
> data/execution layer carries over.

## Architecture

| Persona | Model | Provider | Role | Weight |
|---|---|---|---|---|
| Kisuke | Claude Fable 5 | Vertex AI (global) | Senior analyst | 1.3 |
| Aizen | Claude Sonnet 5 | Vertex AI (global) | Adversarial skeptic | 1.0 |
| Gojo | Grok 4.5 | xAI | Flow / momentum | 1.0 |
| Triage | Claude Haiku 4.5 | Vertex AI | Pre-filter (no vote) | — |

Entries and exits require an unweighted 2-of-3 vote; Kisuke's 1.3 weight only
moves consensus math (probability, edge, sizing, fair value), never vote counts.
Each run makes one batched call per agent covering all markets. Market universe
is `crypto` / `sports` / `climate` (Polymarket US categories — there is no
"weather" category; `climate` is the equivalent, and `crypto` is thin, ~13
Bitcoin-threshold markets).

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in values
```

Environment (see `.env.example`):

- `GOOGLE_CLOUD_PROJECT` — GCP project with the Claude models enabled in Model Garden
- `GOOGLE_APPLICATION_CREDENTIALS` — service-account JSON path, or use `gcloud auth application-default login`
- `XAI_API_KEY` — xAI key for Grok
- `POLYMARKET_KEY_ID` / `POLYMARKET_SECRET_KEY` — Polymarket US API credentials (no wallet, no private key, no on-chain gas token)
- `LIVE_TRADING` — keep `false` (paper) unless the operator says otherwise

## Running

```bash
# manual paper run with a forced hunt
FORCE_HUNT=true python3 orchestrator.py

# mechanical-only pass: settlements, hard stops, trailing take-profit — no
# agent calls, no hunting
python3 orchestrator.py --watchdog-only
```

Cron (installed):

```cron
# full desk run, 6x daily UTC; hunts fire automatically on the 0/8/16 runs
0 0,4,8,12,16,20 * * * cd "$HOME/tko-agents/Prophet Council" && set -a && . ./.env && set +a && "$HOME/tko-agents/Prophet Council/.venv/bin/python" orchestrator.py >> orchestrator.log 2>&1

# watchdog-only, every 20 min — mechanical exits between full desk runs
*/20 * * * * cd "$HOME/tko-agents/Prophet Council" && set -a && . ./.env && set +a && "$HOME/tko-agents/Prophet Council/.venv/bin/python" orchestrator.py --watchdog-only >> orchestrator_watchdog.log 2>&1
```

State (bankroll, positions, journal) persists in `orchestrator_state.json`
(gitignored). Logs go to `orchestrator.log` / `orchestrator_watchdog.log` (gitignored).

## Known gaps from the Polymarket US migration

- **No liquidity figure on market objects.** `liquidity_usd` is sourced from
  `volume24hr` as a substitute; `RISK.min_liquidity_usd` now effectively gates
  on 24h volume, not book depth. Flagged, not silently redefined — see
  comments in `fetch_candidate_markets`.

**Resolved (2026-07-12):** `POLYMARKET_SECRET_KEY` was truncated (86 chars,
not a multiple of 4 — invalid base64 padding), causing every authenticated
endpoint to fail. Operator replaced it with the full value; re-verified
end-to-end: `account.balances` / `portfolio.positions` now return `200 OK`
(account is unfunded — empty balances/positions, as expected), and
`orders.preview` round-trips against a real live market. Auth is confirmed
working. Account still has $0 funded, and `LIVE_TRADING` stays `false` until
the operator funds it and says otherwise.
