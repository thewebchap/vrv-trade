"""Trade storage: reference allocation, source PDFs, metadata, master register, local --save-trade."""
import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from fake_trade import GOOD, MISMATCH, PDF, make_result
from openpyxl import Workbook

from tradepilot import cli, processor
from tradepilot import state as st
from tradepilot.trade_store import COLUMNS, TradeStore, register_row, safe_filename


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_STORAGE_PATH", str(tmp_path / "trade"))
    return TradeStore()


@pytest.fixture
def case_dir(tmp_path):
    d = tmp_path / "case"
    d.mkdir()
    for name in ("1. PC.pdf", "2. PI.pdf", "3. BL.pdf", "4. SC.pdf", "5. SI.pdf"):
        (d / name).write_bytes(PDF)
    (d / "notes.txt").write_text("not a pdf")
    return str(d)


@pytest.fixture
def fake_engine(monkeypatch):
    """Replace only the verification engine; processor.process_trade and the report writer stay real."""
    calls = []

    def process_case(source, model=None, today=None, case_id=None):
        calls.append((source, case_id, sorted(os.listdir(source))))
        return make_result(case_id, list(GOOD), sorted(os.listdir(source)))
    monkeypatch.setattr(processor, "process_case", process_case)
    return calls


# ------------------------------------------------------------------ references

def test_reference_format_and_sequence(store):
    refs = [store.allocate(2026) for _ in range(3)]
    assert refs == ["TRD-2026-000001", "TRD-2026-000002", "TRD-2026-000003"]
    assert all(os.path.isdir(store.source_folder(r)) for r in refs)


def test_sequence_continues_after_existing_trades_and_per_year(store):
    os.mkdir(os.path.join(store.root, "TRD-2026-000041"))
    os.mkdir(os.path.join(store.root, "TRD-2025-000900"))
    assert store.allocate(2026) == "TRD-2026-000042"
    assert store.allocate(2027) == "TRD-2027-000001"


def test_concurrent_allocation_never_duplicates(store):
    stores = [TradeStore(store.root) for _ in range(4)]
    with ThreadPoolExecutor(8) as pool:
        refs = list(pool.map(lambda i: stores[i % 4].allocate(2026), range(40)))
    assert len(set(refs)) == 40 and sorted(refs)[-1] == "TRD-2026-000040"


def test_invalid_reference_rejected(store):
    for bad in ("../etc", "TRD-2026-1", "TRD-2026-000001/../../x", ""):
        with pytest.raises(ValueError):
            store.folder(bad)


# ------------------------------------------------------------------ source PDFs and metadata

def test_source_pdfs_never_overwritten(store):
    ref = store.allocate()
    a = store.save_source_pdf(ref, "PC.pdf", b"%PDF-first")
    b = store.save_source_pdf(ref, "PC.pdf", b"%PDF-second")
    assert os.path.basename(a) == "PC.pdf" and os.path.basename(b) == "PC_2.pdf"
    assert open(a, "rb").read() == b"%PDF-first"


def test_filenames_sanitised():
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("C:\\Users\\x\\inv.pdf") == "inv.pdf"
    assert safe_filename("a;rm -rf $HOME|.pdf") == "a_rm -rf _HOME_.pdf"
    assert safe_filename("...") == "document" and safe_filename(None) == "document"
    assert len(safe_filename("x" * 500 + ".pdf")) <= 120


def test_metadata_round_trip(store):
    ref = store.allocate()
    store.write_metadata(ref, {"trade_ref": ref, "sender": "a@vrvglobal.com"})
    assert store.read_metadata(ref) == {"trade_ref": ref, "sender": "a@vrvglobal.com"}
    assert not [f for f in os.listdir(store.folder(ref)) if f.endswith(".tmp")]


# ------------------------------------------------------------------ master register

def test_register_upsert_keeps_one_row_per_trade(store):
    store.upsert_register({"Trade Ref": "TRD-2026-000001", "Processing Status": "PROCESSING"})
    store.upsert_register({"Trade Ref": "TRD-2026-000002", "Processing Status": "COMPLETED"})
    store.upsert_register({"Trade Ref": "TRD-2026-000001", "Processing Status": "COMPLETED", "Reply Status": "sent"})
    rows = store.register_rows()
    assert [r["Trade Ref"] for r in rows] == ["TRD-2026-000001", "TRD-2026-000002"]
    assert rows[0]["Processing Status"] == "COMPLETED" and rows[0]["Reply Status"] == "sent"
    assert list(rows[0]) == COLUMNS
    assert not [f for f in os.listdir(store.root) if f.endswith(".tmp")]


def test_register_preserves_existing_rows_and_adds_missing_columns(store):
    wb = Workbook()
    ws = wb.active
    ws.append(["Trade Ref", "Notes"])
    ws.append(["TRD-2025-000007", "entered by hand"])
    wb.save(store.register_path)
    store.upsert_register({"Trade Ref": "TRD-2026-000001", "Overall Status": "PASS"})
    rows = {r["Trade Ref"]: r for r in store.register_rows()}
    assert rows["TRD-2025-000007"]["Notes"] == "entered by hand"
    assert rows["TRD-2026-000001"]["Overall Status"] == "PASS"


def test_register_row_counts_and_documents():
    res = make_result("TRD-2026-000003", MISMATCH, ["PC.pdf", "PI.pdf", "COA.pdf"])
    row = register_row("TRD-2026-000003", res, {"source": "email", "overall_status": "MISMATCH"}, "/t/TRD")
    assert row["Purchase Contract"] == "PC.pdf" and row["Sales Invoice"] == "missing"
    assert row["Other Documents"] == "COA.pdf" and row["Mismatch Count"] == 1 and row["Pass Count"] == 2 and row["Pass With Note Count"] == 1
    assert row["Payment Term"] == "METALS-2"


# ------------------------------------------------------------------ local mode

def test_local_save_trade_matches_email_layout(store, case_dir, fake_engine):
    ref, res = processor.save_local_trade(store, case_dir)
    folder = store.folder(ref)
    assert sorted(os.listdir(store.source_folder(ref))) == ["1. PC.pdf", "2. PI.pdf", "3. BL.pdf", "4. SC.pdf", "5. SI.pdf"]
    for f in ("result.json", "report.md", "report.xlsx", "email_response.txt", "trade_metadata.json"):
        assert os.path.exists(os.path.join(folder, f)), f
    assert fake_engine[0][1] == ref                       # the trade ref is the case id in all reports
    meta = json.load(open(os.path.join(folder, "trade_metadata.json")))
    assert meta["source"] == "local" and meta["processing_status"] == "COMPLETED" and meta["sender"] is None
    [row] = store.register_rows()
    assert row["Trade Ref"] == ref and row["Source"] == "local" and row["PDF Count"] == 5
    assert f"Trade Ref: {ref}" in open(os.path.join(folder, "email_response.txt")).read()


def test_local_failure_recorded_as_failed(store, case_dir, monkeypatch):
    monkeypatch.setattr(processor, "process_case", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bad pdf")))
    with pytest.raises(RuntimeError):
        processor.save_local_trade(store, case_dir)
    [row] = store.register_rows()
    assert row["Processing Status"] == "FAILED" and row["Overall Status"] == "FAILED" and "bad pdf" in row["Notes"]
    assert os.path.isdir(store.source_folder(row["Trade Ref"]))


def test_cli_save_trade(store, case_dir, fake_engine, capsys):
    assert cli.main([case_dir, "--save-trade", "-q"]) == 0
    assert "TRD-" in capsys.readouterr().out and len(store.register_rows()) == 1


def test_cli_local_mode_unchanged_without_flag(store, case_dir, tmp_path, monkeypatch):
    from tradepilot import cli as cli_mod
    monkeypatch.setattr(cli_mod, "process_case", lambda path, model, today: make_result("case", list(GOOD)))
    out = tmp_path / "out"
    assert cli.main([case_dir, "--out", str(out), "-q"]) == 0
    assert (out / "case" / "result.json").exists()
    assert store.register_rows() == [] and not [d for d in os.listdir(store.root) if d.startswith("TRD-")]


# ------------------------------------------------------------------ state DB

def test_state_records_and_blocks_duplicates(tmp_path):
    s = st.State(str(tmp_path / "s.db"))
    assert s.should_process("m1")
    s.save("m1", st.PROCESSING, trade_ref="TRD-2026-000001", sender="a@vrvglobal.com")
    assert s.should_process("m1")                                 # an interrupted run is resumed
    s.save("m1", st.COMPLETED, replied_at=st.now_utc(), reply_status="sent")
    row = s.get("m1")
    assert not s.should_process("m1") and row["trade_ref"] == "TRD-2026-000001" and row["sender"] == "a@vrvglobal.com"
    s.save("m2", st.IGNORED_SENDER, sender="x@evil.com")
    assert not s.should_process("m2")
    for _ in range(st.MAX_ATTEMPTS):
        s.save("m3", st.PROCESSING)
        s.save("m3", st.FAILED, error="Graph 503")
    assert not s.should_process("m3")
    assert s.start_time() == s.start_time() and [r["graph_message_id"] for r in s.rows()] == ["m1", "m2", "m3"]
