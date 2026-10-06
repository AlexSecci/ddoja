import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.store import Store
from app.workers import work_once

parser = argparse.ArgumentParser()
parser.add_argument('stage', choices=['evaluation', 'materials', 'review'])
parser.add_argument('--root', type=Path, default=ROOT)
parser.add_argument('--codex')
options = parser.parse_args()
print(json.dumps(work_once(Store(options.root), options.stage, options.codex), indent=2))
