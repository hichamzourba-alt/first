"""Bridge between Harvey's outbox and the Zourba-Tech company repo's OUTBOX/.

Zourba-Tech runs on one approval rule: nothing goes out without Hicham, and he
approves outbound work in batch from ``OUTBOX/`` (see that folder's README).
This module makes Harvey a citizen of that queue instead of a second inbox:

  export   Harvey's ``pending_review`` emails become ``OUTBOX/*.md`` files
           with ``status: pending``.
  import   A file Hicham set to ``approved`` approves the Harvey item (any
           subject/body edits he made between the markers ride along), so
           Harvey's sender sends it on schedule — through the same
           deterministic pre-send gate as every other email. ``rejected``
           rejects it.
  report   Once Harvey has sent, cancelled or failed an item, the file's
           status is updated; sent files move to ``OUTBOX/sent/``.

The files are the only shared state. A file is linked to its outbox row by
its ``harvey-id:`` header, so ``sync`` is stateless and safe to re-run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from harvey.state import StateManager

BODY_START = "<!-- harvey:body -->"
BODY_END = "<!-- /harvey:body -->"

# Harvey outbox status → OUTBOX file status, for items that are finished.
_TERMINAL = {"sent": "sent", "cancelled": "cancelled", "failed": "failed", "rejected": "rejected"}

_HEADER_RE = re.compile(r"^([a-z][a-z-]*):[ \t]*(.*)$")


@dataclass
class SyncReport:
    exported: list[str] = field(default_factory=list)
    approved: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)   # sent/cancelled/failed written back
    problems: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"exported {len(self.exported)}, approved {len(self.approved)}, "
            f"rejected {len(self.rejected)}, closed {len(self.closed)}, "
            f"problems {len(self.problems)}"
        )


# ── file format ──

def _slug(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:limit].strip("-") or "prospect"


def parse_headers(text: str) -> dict[str, str]:
    """Read the ``key: value`` block at the top of an OUTBOX file."""
    headers: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            break
        m = _HEADER_RE.match(line)
        if m:
            headers[m.group(1)] = m.group(2).strip()
        elif not line.startswith((" ", "\t")):
            break
    return headers


def set_header(text: str, key: str, value: str) -> str:
    """Replace one header line, or add it to the end of the header block."""
    lines = text.splitlines(keepends=True)
    end = 0
    for i, line in enumerate(lines):
        if not line.strip():
            break
        if _HEADER_RE.match(line.rstrip("\n")) or line.startswith((" ", "\t")):
            end = i + 1
            if line.startswith(f"{key}:"):
                lines[i] = f"{key}: {value}\n"
                return "".join(lines)
        else:
            break
    lines.insert(end, f"{key}: {value}\n")
    return "".join(lines)


def parse_body(text: str) -> str | None:
    """The email body between the markers, or None when they're missing."""
    start, end = text.find(BODY_START), text.find(BODY_END)
    if start == -1 or end == -1 or end < start:
        return None
    return text[start + len(BODY_START):end].strip("\n")


def render_file(item: dict, prospect=None, company: str = "") -> str:
    name = prospect.full_name() if prospect else ""
    title = prospect.title if prospect else ""
    company = company or (prospect.company if prospect else "")
    who = ", ".join(x for x in (name, title) if x)
    to = f"{who} — {company} — {item['to_email']}" if who else item["to_email"]
    notes = prospect.personalization_notes if prospect else ""
    send_at = (item.get("send_at") or "")[:16]

    lines = [
        f"to: {to}",
        "channel: email",
        "status: pending",
        f"harvey-id: {item['id']}",
        f"subject: {item['subject']}",
        f"send-after: {send_at} UTC" if send_at else "send-after: on approval",
        "",
        f"> Owner: Harvey (02-sales pipeline). Step {item.get('step', 1)} "
        f"({item.get('kind', 'sequence')}). Generated {datetime.now(timezone.utc):%Y-%m-%d}.",
    ]
    if notes:
        lines.append(f"> Research: {notes}")
    lines += [
        "> To approve: set `status: approved` (edit `subject:` or the body between the",
        "> markers first if you want; edits are sent). To kill it: `status: rejected`.",
        "> Approved emails are sent automatically by Harvey on its next cycle, with the",
        "> opt-out footer from harvey.yaml (`channels.email.footer`) added below the body.",
        "",
        "---",
        "",
        BODY_START,
        item["body"].rstrip("\n"),
        BODY_END,
        "",
    ]
    return "\n".join(lines)


# ── sync ──

def _index(outbox_dir: Path) -> dict[str, Path]:
    """Every OUTBOX file (any subfolder) that carries a harvey-id."""
    found: dict[str, Path] = {}
    for path in outbox_dir.rglob("*.md"):
        if path.name.lower() == "readme.md":
            continue
        try:
            hid = parse_headers(path.read_text(encoding="utf-8")).get("harvey-id")
        except (OSError, UnicodeDecodeError):
            continue
        if hid:
            found[hid] = path
    return found


async def sync(state: StateManager, outbox_dir: str | Path, dry_run: bool = False) -> SyncReport:
    outbox = Path(outbox_dir)
    if not outbox.is_dir():
        raise FileNotFoundError(f"OUTBOX folder not found: {outbox}")
    report = SyncReport()
    files = _index(outbox)

    # 1. Decisions in the files → Harvey.
    for hid, path in files.items():
        text = path.read_text(encoding="utf-8")
        status = parse_headers(text).get("status", "").lower()
        item = await state.get_outbox_item(hid)
        if item is None:
            report.problems.append(f"{path.name}: no Harvey outbox item {hid}")
            continue
        if item["status"] != "pending_review":
            continue
        if status == "approved":
            subject = parse_headers(text).get("subject") or item["subject"]
            body = parse_body(text)
            if body is None:
                report.problems.append(f"{path.name}: body markers missing; not approved")
                continue
            if not dry_run:
                if subject != item["subject"] or body != item["body"]:
                    await state.update_outbox_item(hid, subject=subject, body=body)
                await state.approve_outbox(hid)
                await state.log_action("outbox_approved", "zourba", {"id": hid, "file": path.name})
            report.approved.append(path.name)
        elif status == "rejected":
            if not dry_run:
                await state.update_outbox_item(hid, status="rejected", error="rejected in OUTBOX")
                await state.log_action("outbox_rejected", "zourba", {"id": hid, "file": path.name})
            report.rejected.append(path.name)

    # 2. Finished Harvey items → their files.
    for hid, path in files.items():
        item = await state.get_outbox_item(hid)
        if item is None or item["status"] not in _TERMINAL:
            continue
        new_status = _TERMINAL[item["status"]]
        text = path.read_text(encoding="utf-8")
        if parse_headers(text).get("status", "").lower() == new_status:
            continue
        text = set_header(text, "status", new_status)
        if item["status"] == "sent" and item.get("sent_at"):
            text = set_header(text, "sent-at", f"{item['sent_at'][:16]} UTC")
        if item["status"] in ("cancelled", "failed") and item.get("error"):
            text = set_header(text, "harvey-note", item["error"][:200])
        report.closed.append(f"{path.name} → {new_status}")
        if dry_run:
            continue
        path.write_text(text, encoding="utf-8")
        if new_status == "sent" and path.parent.name != "sent":
            dest = outbox / "sent" / path.name
            dest.parent.mkdir(exist_ok=True)
            path.rename(dest)

    # 3. New Harvey drafts → files.
    for item in await state.get_outbox(status="pending_review", limit=500):
        if item["id"] in files:
            continue
        prospect = await state.get_prospect(item["prospect_id"]) if item.get("prospect_id") else None
        company = ""
        if prospect and prospect.company_id:
            c = await state.get_company(prospect.company_id)
            company = c.name if c else ""
        day = (item.get("created_at") or "")[:10] or f"{datetime.now(timezone.utc):%Y-%m-%d}"
        name = (
            f"{day}-sales-harvey-{_slug(company or item['to_email'].split('@')[-1])}"
            f"-s{item.get('step', 1)}-{item['id'][:6]}.md"
        )
        if not dry_run:
            (outbox / name).write_text(render_file(item, prospect, company), encoding="utf-8")
        report.exported.append(name)

    return report
