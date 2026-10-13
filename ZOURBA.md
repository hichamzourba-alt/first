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
| `harvey zourba sync`: bridge to the company repo — suppression list, Sales' skill files, drafts out to `OUTBOX/`, approvals back in, sent status written back | `harvey/zourba.py`, `harvey/cli.py`, `tests/test_zourba.py` |
| `harvey zourba setup`: one-time — confirms every free signal, then syncs | `harvey/cli.py` |
| Pre-send gate blocks first-touch mail to free-mail/consumer domains (gmail, telenet.be, ziggo.nl…) | `harvey/gate.py` |
| `harvey forget <email>`: GDPR erasure of one person | `harvey/state.py`, `harvey/cli.py` |

All facts in `harvey.yaml` come from `zourba-company/COMPANY.md` (commercial model,
markets, language policy). If the charter changes, update the YAML.

## Setup (once, on Hicham's machine)

Clone both repos side by side, then:

```bash
cd first
python3.13 -m venv .venv && source .venv/bin/activate && pip install -e .
cp .env.example .env          # then fill in the three items below
harvey gmail auth             # log in as sales@zourba-tech.be
harvey zourba setup           # confirms free signals, pulls Sales' skill files
harvey discover               # free OpenStreetMap source, BE/NL cities
harvey run                    # heartbeat loop
```

In `.env`:

1. `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET`: Google Cloud Console, enable the Gmail API,
   create an OAuth client of type "Desktop app". The mailbox is **sales@zourba-tech.be**;
   it needs SPF, DKIM and DMARC on zourba-tech.be.
2. `REOON_API_KEY`: free tier at reoon.com. Without a verifier, addresses stay `guess`
   and are never sent to.
3. `ZOURBA_OUTBOX=/path/to/zourba-company/OUTBOX`.

**No `harvey train`.** zourba-tech.be's content is already captured, more accurately, in
the company repo's sales docs. Product knowledge and battle cards live in
`zourba-company/02-sales/work/harvey/` (private) and every `harvey zourba sync` copies
them into `skills/`. This repo is public, so rates and positioning stay out of it.

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
- Every sync first applies `06-cold-callers/work/SUPPRESSION.md`: listed emails and
  domains are stopped and their queued mail cancelled. Opt-outs Harvey receives are
  appended to that list the same day.
- `harvey zourba sync --dry-run` shows what would change without writing.
- `harvey sending pause` stops everything immediately.
- Approving in Harvey's own dashboard (`harvey dashboard`) also works; the next sync
  writes the `sent` status back to the file.

Run `harvey zourba sync` before and after the daily run (or on a cron next to
`harvey run`) so the digest sees new drafts and Hicham's approvals go out.

## Compliance

Reviewed against Zourba-Tech's GDPR rules in
`zourba-company/10-security/work/HARVEY-GDPR-REVIEW-2026-10-13.md`. In short: business
addresses only (free-mail domains blocked by the gate), source URL on every fact,
suppression list enforced both ways, opt-out footer on every outreach email (don't empty
`channels.email.footer`), erasure with `harvey forget <email>`, and the database stays
local (`data/` is gitignored).
