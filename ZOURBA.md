# Harvey for Zourba-Tech

Harvey finds BE/NL MSPs, reads their websites for a specific hook, writes a Dutch
cold email per prospect, and — **only after Hicham approves it** — sends it and handles
the reply. This file covers what is different from upstream Harvey.

## What changed from upstream

| Change | Where |
|---|---|
| Zourba-Tech persona, offer, ICP (BE/NL MSPs, 10–100 staff), hiring signals, Brussels quiet hours, 20 sends/day while warming up, LinkedIn off | `harvey.yaml` |
| `persona.language` setting; the writer writes every email in it (default `English`, set to `Dutch`) | `harvey/config.py`, `harvey/agents/writer.py`, `prompts/writer.md` |
| `channels.email.footer`: Dutch sender + opt-out line appended to every outreach email at send time | `harvey/config.py`, `harvey/agents/sender.py` |
| Reply handler answers in the prospect's language | `prompts/handler.md` |
| `harvey zourba sync`: bridge to the company repo's `OUTBOX/` | `harvey/zourba.py`, `harvey/cli.py`, `tests/test_zourba.py` |

All facts in `harvey.yaml` come from `zourba-company/COMPANY.md` (commercial model,
markets, language policy). If the charter changes, update the YAML.

## Setup (once, on Hicham's machine)

```bash
git clone <this repo> harvey && cd harvey
python3.13 -m venv .venv && source .venv/bin/activate && pip install -e .
cp .env.example .env
```

In `.env`:

1. **Mailbox.** `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET`, then `harvey gmail auth`.
   Use a dedicated outreach mailbox/domain with SPF, DKIM and DMARC — not the main
   `zourba-tech.be` inbox. Update `persona.email` in `harvey.yaml` to match.
2. **Verifier.** `REOON_API_KEY` (free tier). Without one, addresses stay `guess` and
   are never sent to.
3. **`ZOURBA_OUTBOX`** = absolute path to `zourba-company/OUTBOX`.

Then:

```bash
harvey train https://zourba-tech.be   # writes skills/product_knowledge.md
harvey signals --confirm free         # what to look for (costs nothing)
harvey discover                       # free OpenStreetMap source, BE/NL cities
harvey run                            # heartbeat loop
```

Per the charter, spending money (paid discovery providers, verifier upgrades) needs
Hicham's yes first. The defaults above cost nothing.

## The approval flow

```
Harvey writes draft ─► harvey zourba sync ─► OUTBOX/<date>-sales-harvey-<co>-s1-<id>.md
                                              status: pending
                                                     │  Hicham edits (optional) and sets
                                                     ▼  status: approved | rejected
                       harvey zourba sync ◄──────────┘
                              │ approved → Harvey outbox approved (edits applied)
                              ▼
            Harvey sender: pre-send gate → Gmail → status: sent, file → OUTBOX/sent/
```

- Each file carries `harvey-id:`, `subject:`, and the body between
  `<!-- harvey:body -->` markers. Edit those freely before approving; the edited
  version is what gets sent. If the markers are removed, the approval is refused and
  reported as a problem.
- Approved emails still pass Harvey's deterministic gate (banned phrases, length,
  unrendered merge tags, deliverable address) and respect the daily cap, quiet hours,
  stop-on-reply and the bounce kill switch.
- `harvey zourba sync --dry-run` shows what would change without writing.
- `harvey sending pause` stops everything immediately.
- Approving in Harvey's own dashboard (`harvey dashboard`) also works; the next sync
  writes the `sent` status back to the file.

Run `harvey zourba sync` before and after the daily run (or on a cron next to
`harvey run`) so the digest sees new drafts and Hicham's approvals go out.

## Compliance

Outreach to BE/NL businesses falls under GDPR and the ePrivacy rules: B2B cold email
to a role address or a named decision-maker needs a legitimate-interest basis, a clear
sender identity, and a working opt-out in every email. `channels.email.footer` in
`harvey.yaml` adds that line to every outreach email; don't empty it. Have Mehdi (10-security) sign off
on the data Harvey stores in `data/harvey.db` before the first send.
