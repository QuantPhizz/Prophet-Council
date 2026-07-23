# PROPHET COUNCIL CLAUDE.md — Tier 3
# Owner: Nick (shugogeta)
# Location: ~/tko-agents/Prophet Council/CLAUDE.md
# Scope: Sessions working inside this project only
# Last Updated: July 2026

---

## WHAT THIS PROJECT IS

Prophet Council is a Polymarket multi-agent trading desk (originally handed off
as "polydesk"). A single Python orchestrator (`orchestrator.py`) runs a
three-agent analyst desk plus a triage pre-filter over Polymarket **US**
markets (crypto, sports, climate only), paper-trading by default.

Migrated from Polymarket International to Polymarket US on 2026-07-11 (see
`SPEC_polymarket_us_migration.md`) — these are separate platforms with
unrelated order books/prices. Data reads and order execution both go through
the `polymarket-us` SDK now; there is no wallet, private key, or on-chain gas
token anywhere in this project.

The desk (runtime personas, NOT Claude Code subagents — never create
.claude/agents entries for them):

- Kisuke — Claude Fable 5 (Vertex AI, global) — senior analyst, weight 1.3
- Aizen  — Claude Sonnet 5 (Vertex AI, global) — adversarial skeptic, weight 1.0
- Gojo   — Grok 4.5 (xAI) — flow/momentum outside voice, weight 1.0
- Triage — Claude Haiku 4.5 (Vertex AI) — cheap pre-filter, no vote

Persona files live in `personas/` and must sit alongside `orchestrator.py`
(paths are relative). Full operational details are in `HANDOFF.md`.

## KEY MECHANICS (do not change without owner instruction)

- 2-of-3 unweighted vote required for entries and exits; Kisuke's 1.3 weight
  moves consensus numbers (probability, edge, sizing, fair value), never votes.
- Consolidated calls: one batched API call per agent per run.
- Cron cadence: 6 full desk runs/day UTC (0,4,8,12,16,20); hunts only on 0/8/16.
  Separately, `watchdog_dynamic.sh` fires every 2 min but only actually runs
  `orchestrator.py --watchdog-only` every 2 min while a position is open —
  idle, it throttles itself back to the :00/:20/:40 tick (~20 min).
- RISK parameters, agent weights, and personas are LOCKED — owner changes only.

## SECRETS & SAFETY

- LIVE_TRADING stays "false" until the operator explicitly says otherwise.
- POLYMARKET_KEY_ID / POLYMARKET_SECRET_KEY are the Polymarket US API
  credentials (Ed25519 signing handled internally by the SDK). No wallet, no
  private key, no chain id.
- Env vars: GOOGLE_CLOUD_PROJECT, GOOGLE_APPLICATION_CREDENTIALS (or ADC),
  XAI_API_KEY, POLYMARKET_KEY_ID, POLYMARKET_SECRET_KEY, LIVE_TRADING,
  FORCE_HUNT. Local `.env` only, never committed.
- `.env`, `orchestrator_state.json`, and `*.log` are gitignored.

## RUNNING

- Venv: `.venv/` in this directory; `pip install -r requirements.txt`.
- Manual paper run: `FORCE_HUNT=true python3 orchestrator.py`
- State persists in `orchestrator_state.json` (cash, positions, daily P&L,
  journal). Delete it only if the owner wants a bankroll reset.

## GIT

- Own private repo (Prophet-Council). Standard three-branch model:
  main (owner-push), develop (agent output), feature/* (per-task).
- Never commit secrets or runtime state.
