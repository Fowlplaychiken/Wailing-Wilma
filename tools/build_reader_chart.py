"""Build a single offline HTML chart from the public experiment ledger."""
import argparse
import csv
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "docs/reader-trials.csv"


def build(output):
    with LEDGER.open(encoding="utf-8-sig", newline="") as source:
        records = [{key: value for key, value in row.items() if value} for row in csv.DictReader(source)]
    payload = json.dumps(records, ensure_ascii=True, separators=(",", ":")).replace("<", r"\u003c")
    template = Path(__file__).with_name("reader_chart.html").read_text(encoding="utf-8")
    if template.count("__LEDGER_JSON__") != 1:
        raise ValueError("Expected one chart data slot")
    output.write_text(template.replace("__LEDGER_JSON__", payload).replace(
        "__LEDGER_SHA__", hashlib.sha256(LEDGER.read_bytes()).hexdigest()[:12]), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="Standalone .html file to generate")
    build(parser.parse_args().output)
