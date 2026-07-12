# HANDOFF BRIEF — Polymarket Multi-Agent Desk ("polydesk")

Audience: Claude Code terminal agent. Read fully before acting.

> **MIGRATION NOTE (2026-07-11):** This handoff describes the original build
> against Polymarket International (polymarket.com). The desk has since been
> migrated to Polymarket US (polymarket.us) — see
> `SPEC_polymarket_us_migration.md` and the `CLAUDE.md` in this directory for
> current facts. `POLY_PRIVATE_KEY`/`py-clob-client` below are historical and
> no longer apply; current execution uses `POLYMARKET_KEY_ID` /
> `POLYMARKET_SECRET_KEY` via the `polymarket-us` SDK.

## ⚠️ Environment guardrails — read first
- The operator has an EXISTING set of "storm team" aliases (shell aliases and/or
  Claude Code subagent definitions). DO NOT modify, rename, overwrite, or remove any
  existing aliases, ~/.claude/agents entries, shell rc files, or crontab lines that
  are not created by this project.
- Namespace everything under a dedicated project directory (suggested: ~/polydesk/).
  Do not define new shell aliases for this project; use explicit paths in cron.
- When editing crontab, APPEND the new line only. Print the existing crontab first
  and confirm nothing else changed.
- Kisuke / Aizen / Gojo are runtime personas inside orchestrator.py — they are NOT
  Claude Code subagents. Do not create .claude/agents entries for them.

## Files in this handoff
- orchestrator.py          — the full bot (entry, exit, triage, batched desk calls)
- personas/kisuke.md       — Fable 5, senior analyst, weight 1.3
- personas/aizen.md        — Sonnet 5, adversarial skeptic
- personas/gojo.md         — Grok 4.5, flow/momentum outside voice
The personas/ directory must sit alongside orchestrator.py (paths are relative).

## Setup tasks (in order)
1. mkdir ~/polydesk && place files; git init and commit before any changes.
2. python3 -m venv .venv && source .venv/bin/activate
3. pip install "anthropic[vertex]" openai py-clob-client requests
4. Env (use a .env or systemd/cron env file; NEVER commit secrets, add .env + 
   orchestrator_state.json + *.log to .gitignore):
   - GOOGLE_CLOUD_PROJECT=<project id>
   - GOOGLE_APPLICATION_CREDENTIALS=<path to service-account json>
     (or `gcloud auth application-default login`)
   - XAI_API_KEY=<key>
   - LIVE_TRADING=false   # keep false until operator explicitly says otherwise
5. Verify Vertex model IDs in Model Garden match the AGENTS config strings
   (claude-fable-5, claude-sonnet-5, claude-haiku-4-5). Some listings are
   versioned (name@YYYYMMDD) — update config to the exact ID, region stays "global".
6. Smoke test: FORCE_HUNT=true python3 orchestrator.py
   Expected: candidate fetch -> triage log -> 3 batched desk calls -> paper entries
   written to orchestrator_state.json. Fix any auth/model-ID errors before cron.
7. Cron (append only):
   0 0,4,8,12,16,20 * * * cd $HOME/polydesk && $HOME/polydesk/.venv/bin/python orchestrator.py >> orchestrator.log 2>&1

## Acceptance checks before reporting done
- [ ] Existing crontab lines and all pre-existing aliases untouched (diff shown)
- [ ] Paper run produced valid JSON opinions from all three agents (check journal)
- [ ] Malformed-agent-response path tested (forced PASS, run continues)
- [ ] orchestrator_state.json persists cash/positions across two runs
- [ ] Secrets not committed; .gitignore verified

## Explicitly out of scope for the agent
- Setting LIVE_TRADING=true or handling POLY_PRIVATE_KEY (operator does this manually)
- Changing RISK parameters, agent weights, or personas without operator instruction
