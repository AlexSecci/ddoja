"""Independent, local feature switches used by the efficiency ablations."""
import json
from pathlib import Path

DEFAULTS = dict(persistent_browser=True, batch_form_edits=True, compact_worker_context=True,
                document_edits=True, isolated_form_slots=True)


def features(root):
    try:
        value = json.loads((Path(root)/'config/efficiency.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        value = {}
    return {key: value.get(key, default) if type(value.get(key, default)) is bool else default
            for key, default in DEFAULTS.items()}
