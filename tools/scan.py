"""Scan a previously configured and enabled company. No application submission."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.discovery import scan_company
from app.store import Store

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("company_id")
    args = parser.parse_args()
    print(json.dumps(scan_company(Store(Path(__file__).resolve().parents[1]), args.company_id), indent=2))
