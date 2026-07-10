# Prophet Council

Multi-agent Polymarket trading desk. Three LLM analyst personas (Kisuke, Aizen,
Gojo) plus a cheap triage pre-filter analyze crypto / sports / weather
prediction markets and paper-trade a shared bankroll. Not financial advice.

## Architecture

| Persona | Model | Provider | Role | Weight |
|---|---|---|---|---|
| Kisuke | Claude Fable 5 | Vertex AI (global) | Senior analyst | 1.3 |
| Aizen | Claude Sonnet 5 | Vertex AI (global) | Adversarial skeptic | 1.0 |
| Gojo | Grok 4.5 | xAI | Flow / momentum | 1.0 |
| Triage | Claude Haiku 4.5 | Vertex AI | Pre-filter (no vote) | — |

Entries and exits require an unweighted 2-of-3 vote; Kisuke's 1.3 weight only
moves consensus math (probability, edge, sizing, fair value), never vote counts.
Each run makes one batched call per agent covering all markets.

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
- `LIVE_TRADING` — keep `false` (paper) unless the operator says otherwise
- `POLY_PRIVATE_KEY` — live trading only; operator-managed

## Running

```bash
# manual paper run with a forced hunt
FORCE_HUNT=true python3 orchestrator.py
```

Cron (6x daily UTC; hunts fire automatically on the 0/8/16 runs):

```cron
0 0,4,8,12,16,20 * * * cd "$HOME/tko-agents/Prophet Council" && "$HOME/tko-agents/Prophet Council/.venv/bin/python" orchestrator.py >> orchestrator.log 2>&1
```

State (bankroll, positions, journal) persists in `orchestrator_state.json`
(gitignored). Logs go to `orchestrator.log` (gitignored).
