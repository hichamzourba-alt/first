"""Tests for the Zourba-Tech OUTBOX bridge: export, approve (with edits),
reject, and write-back of sent status."""

import os
import re
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.state import StateManager
from harvey import zourba
from harvey.zourba import parse_body, parse_headers, set_header, sync


@pytest_asyncio.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        outbox = Path(tmpdir) / "OUTBOX"
        outbox.mkdir()
        (outbox / "README.md").write_text("status: not-a-draft\n")
        skills = Path(tmpdir) / "skills"
        skills.mkdir()
        orig = zourba.PROJECT_ROOT
        zourba.PROJECT_ROOT = Path(tmpdir)  # never touch the real skills/
        try:
            yield sm, outbox
        finally:
            zourba.PROJECT_ROOT = orig


async def _queue(sm, email="d.jansen@acme-it.nl", body="Beste Dirk,\n\nHoe vangen jullie de avonduren op?"):
    company_id = await sm.add_company(Company(name="Acme IT", domain="acme-it.nl"))
    prospect_id = await sm.add_prospect(Prospect(
        company_id=company_id, first_name="Dirk", last_name="Jansen",
        title="CTO", email=email, email_status="verified",
    ))
    return await sm.add_outbox_item(
        prospect_id=prospect_id, to_email=email, subject="after-hours dekking",
        body=body, send_at="2026-10-14T08:30:00", campaign_id="c1",
    )


def _only_draft(outbox: Path) -> Path:
    files = [p for p in outbox.rglob("*.md") if p.name != "README.md"]
    assert len(files) == 1
    return files[0]


@pytest.mark.asyncio
async def test_export_writes_pending_file(env):
    sm, outbox = env
    item_id = await _queue(sm)
    report = await sync(sm, outbox)
    assert len(report.exported) == 1
    path = _only_draft(outbox)
    assert "-sales-harvey-acme-it-s1-" in path.name
    text = path.read_text()
    headers = parse_headers(text)
    assert headers["status"] == "pending"
    assert headers["channel"] == "email"
    assert headers["harvey-id"] == item_id
    assert "Dirk Jansen" in headers["to"] and "Acme IT" in headers["to"]
    assert parse_body(text) == "Beste Dirk,\n\nHoe vangen jullie de avonduren op?"

    # Idempotent: a second sync exports nothing new.
    again = await sync(sm, outbox)
    assert again.exported == []
    _only_draft(outbox)


@pytest.mark.asyncio
async def test_approval_with_edits_reaches_harvey(env):
    sm, outbox = env
    item_id = await _queue(sm)
    await sync(sm, outbox)
    path = _only_draft(outbox)
    text = set_header(path.read_text(), "status", "approved")
    text = set_header(text, "subject", "avonduren")
    text = text.replace("Hoe vangen", "Hoe regelen")
    path.write_text(text)

    report = await sync(sm, outbox)
    assert report.approved == [path.name]
    item = await sm.get_outbox_item(item_id)
    assert item["status"] == "approved"
    assert item["subject"] == "avonduren"
    assert "Hoe regelen" in item["body"]


@pytest.mark.asyncio
async def test_rejection(env):
    sm, outbox = env
    item_id = await _queue(sm)
    await sync(sm, outbox)
    path = _only_draft(outbox)
    path.write_text(set_header(path.read_text(), "status", "rejected"))
    report = await sync(sm, outbox)
    assert report.rejected == [path.name]
    assert (await sm.get_outbox_item(item_id))["status"] == "rejected"


@pytest.mark.asyncio
async def test_missing_markers_blocks_approval(env):
    sm, outbox = env
    item_id = await _queue(sm)
    await sync(sm, outbox)
    path = _only_draft(outbox)
    text = set_header(path.read_text(), "status", "approved").replace("<!-- /harvey:body -->", "")
    path.write_text(text)
    report = await sync(sm, outbox)
    assert report.approved == [] and len(report.problems) == 1
    assert (await sm.get_outbox_item(item_id))["status"] == "pending_review"


@pytest.mark.asyncio
async def test_sent_is_written_back_and_moved(env):
    sm, outbox = env
    item_id = await _queue(sm)
    await sync(sm, outbox)
    path = _only_draft(outbox)
    path.write_text(set_header(path.read_text(), "status", "approved"))
    await sync(sm, outbox)
    await sm.update_outbox_item(item_id, status="sent", sent_at="2026-10-14T08:31:00")

    report = await sync(sm, outbox)
    assert len(report.closed) == 1
    assert not path.exists()
    moved = outbox / "sent" / path.name
    headers = parse_headers(moved.read_text())
    assert headers["status"] == "sent"
    assert headers["sent-at"] == "2026-10-14T08:31 UTC"


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(env):
    sm, outbox = env
    await _queue(sm)
    report = await sync(sm, outbox, dry_run=True)
    assert len(report.exported) == 1
    assert [p.name for p in outbox.rglob("*.md")] == ["README.md"]


def test_set_header_adds_missing_key():
    text = "to: x\nstatus: pending\n\nbody\n"
    out = set_header(text, "sent-at", "now")
    assert parse_headers(out) == {"to": "x", "status": "pending", "sent-at": "now"}
    assert out.endswith("\nbody\n")


def _dashboard_decide(path: Path, decision: str) -> Path:
    """Mirror zourba-company/dashboard/server.mjs decideOutbox()."""
    c = path.read_text()
    c = re.sub(r"(?m)^status:.*$", f"status: {decision}", c, count=1)
    note = f"\n> {'APPROVED' if decision == 'approved' else 'DECLINED'} by Hicham via dashboard, 2026-10-13 18:00.\n"
    i = c.index("\n\n")
    c = c[:i] + "\n" + note + c[i:]
    path.write_text(c)
    if decision == "rejected":
        dest = path.parent / "declined" / path.name
        dest.parent.mkdir(exist_ok=True)
        path.rename(dest)
        return dest
    return path


@pytest.mark.asyncio
async def test_dashboard_approve_and_decline_buttons(env):
    sm, outbox = env
    a = await _queue(sm, email="a@acme-it.nl")
    await sync(sm, outbox)
    path = _only_draft(outbox)
    _dashboard_decide(path, "approved")
    await sync(sm, outbox)
    item = await sm.get_outbox_item(a)
    assert item["status"] == "approved"
    assert item["body"] == "Beste Dirk,\n\nHoe vangen jullie de avonduren op?"

    company_id = await sm.add_company(Company(name="Beta ICT", domain="beta.be"))
    pid = await sm.add_prospect(Prospect(company_id=company_id, first_name="An", last_name="Peeters",
                                         title="CEO", email="an@beta.be", email_status="verified"))
    b = await sm.add_outbox_item(prospect_id=pid, to_email="an@beta.be", subject="s", body="b",
                                 send_at="2026-10-14T08:30:00", campaign_id="c2")
    report = await sync(sm, outbox)
    new = outbox / report.exported[0]
    _dashboard_decide(new, "rejected")
    await sync(sm, outbox)
    assert (await sm.get_outbox_item(b))["status"] == "rejected"


@pytest.mark.asyncio
async def test_skills_copied_from_sales_folder(env):
    sm, outbox = env
    src = outbox.parent / "02-sales" / "work" / "harvey"
    src.mkdir(parents=True)
    (src / "product_knowledge.md").write_text("# PK v1\n")
    dest = outbox.parent / "skills" / "product_knowledge.md"

    report = await sync(sm, outbox)
    assert report.skills == ["product_knowledge.md"]
    assert dest.read_text() == "# PK v1\n"
    assert (await sync(sm, outbox)).skills == []          # unchanged → no copy

    (src / "product_knowledge.md").write_text("# PK v2\n")
    assert (await sync(sm, outbox, dry_run=True)).skills == ["product_knowledge.md"]
    assert dest.read_text() == "# PK v1\n"
    await sync(sm, outbox)
    assert dest.read_text() == "# PK v2\n"


SUPPRESSION = """# Suppression List

| Date | Company | Contact | Reason | Logged by |
|---|---|---|---|---|
| — | — | — | — | — |
| 2026-10-01 | Beta ICT (beta.be) | — | deletion request | Lina |
| 2026-10-02 | Gamma | Jo <jo@gamma.nl> | opt-out | Lina |
"""


@pytest.mark.asyncio
async def test_suppression_list_cancels_and_logs(env):
    sm, outbox = env
    sup = outbox.parent / "06-cold-callers" / "work" / "SUPPRESSION.md"
    sup.parent.mkdir(parents=True)
    sup.write_text(SUPPRESSION)

    ok = await _queue(sm)                                    # acme-it.nl: not listed
    beta = await _queue(sm, email="an@beta.be")              # domain listed
    gamma = await _queue(sm, email="jo@gamma.nl")            # email listed
    report = await sync(sm, outbox)
    assert sorted(report.suppressed) == ["an@beta.be", "jo@gamma.nl"]
    assert (await sm.get_outbox_item(ok))["status"] == "pending_review"
    assert (await sm.get_outbox_item(beta))["status"] == "cancelled"
    assert (await sm.get_outbox_item(gamma))["status"] == "cancelled"
    assert len(report.exported) == 1                          # only the clean one
    assert report.logged_opt_outs == []                       # already listed

    # An opt-out Harvey received gets appended once.
    item = await sm.get_outbox_item(ok)
    await sm.update_prospect_status(item["prospect_id"], "opted_out")
    report = await sync(sm, outbox)
    assert report.logged_opt_outs == ["d.jansen@acme-it.nl"]
    text = sup.read_text()
    assert "Dirk Jansen <d.jansen@acme-it.nl> | opt-out (email reply) | Harvey (auto) |" in text
    assert (await sync(sm, outbox)).logged_opt_outs == []
    assert text.count("acme-it.nl") == 1


def test_gate_blocks_free_mail_except_replies():
    from harvey.gate import pre_send_check
    p = Prospect(first_name="A", last_name="B", title="CEO", email="a@telenet.be", email_status="verified")
    blocked = pre_send_check("a@telenet.be", "avonduren", "Beste A, een vraag?", prospect=p)
    assert not blocked and any("personal mailbox" in r for r in blocked.reasons)
    assert pre_send_check("a@telenet.be", "re: avonduren", "Dank je.", prospect=p, kind="reply")


@pytest.mark.asyncio
async def test_forget_prospect_erases_everything(env):
    sm, outbox = env
    item_id = await _queue(sm)
    keep = await _queue(sm, email="other@acme-it.nl")
    await sm.log_action("opt_out", "handler", {"prospect_email": "d.jansen@acme-it.nl"})
    counts = await sm.forget_prospect("D.Jansen@acme-it.nl")
    assert counts["prospects"] == 1 and counts["outbox"] == 1 and counts["actions"] == 1
    assert await sm.get_outbox_item(item_id) is None
    assert await sm.get_prospect_by_email("d.jansen@acme-it.nl") is None
    assert await sm.get_outbox_item(keep) is not None
