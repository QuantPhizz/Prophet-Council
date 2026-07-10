# PROPHET COUNCIL CLAUDE.md — Tier 3
# Owner: Nick (shugogeta)
# Location: ~/tko-agents/Prophet Council/CLAUDE.md
# Scope: Sessions working inside this project only
# Last Updated: July 2026

---

## WHAT THIS PROJECT IS

Prophet Council is a Polymarket multi-agent trading desk (originally handed off
as "polydesk"). A single Python orchestrator (`orchestrator.py`) runs a
three-agent analyst desk plus a triage pre-filter over Polymarket markets
(crypto, sports, weather only), paper-trading by default.

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
- Cron cadence: 6 runs/day UTC (0,4,8,12,16,20); hunts only on 0/8/16.
- RISK parameters, agent weights, and personas are LOCKED — owner changes only.

## SECRETS & SAFETY

- LIVE_TRADING stays "false" until the operator explicitly says otherwise.
- POLY_PRIVATE_KEY is handled by the operator manually — never by an agent.
- Env vars: GOOGLE_CLOUD_PROJECT, GOOGLE_APPLICATION_CREDENTIALS (or ADC),
  XAI_API_KEY, LIVE_TRADING, FORCE_HUNT. Local `.env` only, never committed.
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
