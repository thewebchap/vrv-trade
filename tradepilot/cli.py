"""Command line.

    python tradepilot.py <case folder | folder of cases> [--out output]   verify locally (unchanged)
    python tradepilot.py <case folder> --save-trade                       also store it as a TRD trade
    python tradepilot.py --poll-outlook [--once]                          Outlook email workflow
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date

from .config import log_level
from .local_model import LocalModel
from .pipeline import is_case_dir, process_batch, process_case
from .report import summary_table, write_batch, write_case


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tradepilot", description="Verify metal trade documents against the checklist.")
    ap.add_argument("path", nargs="?", help="a case folder (containing PDFs) or a folder of case folders")
    ap.add_argument("--out", default="output", help="output directory (default: output)")
    ap.add_argument("--as-of", help="evaluation date YYYY-MM-DD (default: today)")
    ap.add_argument("-q", "--quiet", action="store_true")
    ap.add_argument("--save-trade", action="store_true",
                    help="store a single case as a trade under TRADE_STORAGE_PATH (TRD-YYYY-NNNNNN + register row)")
    ap.add_argument("--poll-outlook", action="store_true", help="poll the Outlook inbox and reply to trades")
    ap.add_argument("--once", action="store_true", help="with --poll-outlook: poll once and exit")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.WARNING if args.quiet else getattr(logging, log_level(), logging.INFO),
                        format="%(message)s")
    for noisy in ("urllib3", "requests", "httpx", "msal"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    today = date.fromisoformat(args.as_of) if args.as_of else None
    model = LocalModel()
    if args.poll_outlook:
        from .mailbox import run
        return run(once=args.once, model=model)
    if not args.path:
        ap.error("a case folder is required (or use --poll-outlook)")
    path = os.path.normpath(args.path)
    if not os.path.isdir(path):
        print(f"not a directory: {path}", file=sys.stderr)
        return 2

    if args.save_trade:
        if not is_case_dir(path):
            print("--save-trade needs a single case folder containing PDFs", file=sys.stderr)
            return 2
        from .processor import save_local_trade
        from .trade_store import TradeStore
        store = TradeStore()
        ref, res = save_local_trade(store, path, model=model, today=today)
        print(f"\n{summary_table([res])}\n\nTrade {ref}: {store.folder(ref)}\nRegister: {store.register_path}")
        return 0

    if is_case_dir(path):
        res = process_case(path, model, today)
        out = write_case(res, os.path.join(args.out, res["case_id"]))
        print(f"\n{summary_table([res])}\n\nWritten: {out}/result.json, report.md, report.xlsx")
        return 0

    def on_result(res):
        write_case(res, os.path.join(args.out, res["case_id"]))

    results = process_batch(path, model, today, on_result=on_result)
    if not results:
        print(f"no case folders with PDFs under {path}", file=sys.stderr)
        return 2
    write_batch(results, args.out)
    totals = {}
    for r in results:
        key = "ERROR" if r.get("error") else r["summary"]["overall_status"]
        totals[key] = totals.get(key, 0) + 1
    print(f"\n{summary_table(results)}\n\n{len(results)} cases: " + ", ".join(f"{k} {v}" for k, v in sorted(totals.items()))
          + f"\nWritten: {args.out}/<case>/..., {args.out}/summary.md, summary.json, summary.xlsx")
    return 0
