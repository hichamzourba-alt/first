"""Tests for the Zourba-Tech OUTBOX bridge: export, approve (with edits),
reject, and write-back of sent status."""

import os
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.state import StateManager
from harvey.zourba import parse_body, parse_headers, set_header, sync


@pytest_asyncio.fixture
async def env():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        outbox = Path(tmpdir) / "OUTBOX"
        outbox.mkdir()
        (outbox / "README.md").write_text("status: not-a-draft\n")
        yield sm, outbox


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
