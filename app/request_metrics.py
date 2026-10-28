"""Actual provider-response usage and hash-only cache diagnostics."""
import json
from pathlib import Path
import sqlite3
import time

SCHEMA = '''CREATE TABLE IF NOT EXISTS agent_provider_requests(
 request_id TEXT PRIMARY KEY,worker_id TEXT NOT NULL,run_id TEXT NOT NULL,
 execution TEXT NOT NULL,started_at REAL NOT NULL,finished_at REAL,state TEXT NOT NULL,
 input_tokens INTEGER,cached_input_tokens INTEGER,output_tokens INTEGER,reasoning_tokens INTEGER,
 metadata TEXT NOT NULL DEFAULT '{}');
 CREATE INDEX IF NOT EXISTS provider_requests_worker ON agent_provider_requests(worker_id,run_id,started_at);'''


class RequestMetrics:
    def __init__(self, root):
        self.path = Path(root)/'data/pipeline.sqlite'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=15) as db:db.executescript(SCHEMA)

    def record(self, request_id, worker_id, run_id, execution, started, state, metadata, usage=None):
        values = [None]*4
        if usage:
            values = [usage.get('input_tokens'),(usage.get('input_tokens_details') or {}).get('cached_tokens'),
                      usage.get('output_tokens'),(usage.get('output_tokens_details') or {}).get('reasoning_tokens')]
            if any(v is not None and (type(v) is not int or v < 0) for v in values):
                raise ValueError('Invalid provider usage counters.')
            if values[0] is not None and values[1] is not None and values[1] > values[0]:
                raise ValueError('Provider cached input exceeds total input.')
        with sqlite3.connect(self.path, timeout=15) as db:
            db.execute('INSERT INTO agent_provider_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?) '
                       'ON CONFLICT(request_id) DO UPDATE SET finished_at=excluded.finished_at,state=excluded.state,'
                       'input_tokens=COALESCE(excluded.input_tokens,input_tokens),'
                       'cached_input_tokens=COALESCE(excluded.cached_input_tokens,cached_input_tokens),'
                       'output_tokens=COALESCE(excluded.output_tokens,output_tokens),'
                       'reasoning_tokens=COALESCE(excluded.reasoning_tokens,reasoning_tokens),metadata=excluded.metadata',
                       (request_id,worker_id,run_id,json.dumps(execution,sort_keys=True),started,
                        None if state == 'started' else time.time(),state,*values,json.dumps(metadata,sort_keys=True)))


def effective_rows(db, legacy, pipeline_id=None):
    """Prefer measured HTTP response totals for covered CLI runs; never add both."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='agent_provider_requests'").fetchone():return list(legacy)
    query = ('SELECT r.*,w.role,w.session_id FROM agent_provider_requests r JOIN agent_workers w ON w.id=r.worker_id '
             + ('WHERE w.pipeline_id=? ' if pipeline_id else '')+'ORDER BY r.started_at')
    rows = db.execute(query, (pipeline_id,) if pipeline_id else ()).fetchall()
    groups = {}
    for r in rows:groups.setdefault((r['worker_id'],r['run_id']), []).append(r)
    result = [dict(r) for r in legacy if (r['worker_id'],r['turn_id']) not in groups]
    for (wid,run), requests in groups.items():
        first = requests[0]
        known = [r for r in requests if r['input_tokens'] is not None and r['output_tokens'] is not None]
        # A run whose response never reported usage remains explicitly missing.
        item = dict(worker_id=wid,role=first['role'],session_id=first['session_id'],turn_id=run,
                    counter_source='provider_response',counter_kind='turn',execution=first['execution'],
                    elapsed_seconds=None,tool_call_count=None,created_at=str(first['started_at']),
                    partial_usage=len(known)!=len(requests))
        for name in ('input_tokens','cached_input_tokens','output_tokens','reasoning_tokens'):
            values = [r[name] for r in known if r[name] is not None]
            item['delta_'+name] = sum(values) if values else None
            item[name] = item['delta_'+name]
        result.append(item)
    return result


def diagnostics(db, pipeline_id=None):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='agent_provider_requests'").fetchone():return {'requests':0}
    rows=db.execute('SELECT r.* FROM agent_provider_requests r JOIN agent_workers w ON w.id=r.worker_id '+
                    ('WHERE w.pipeline_id=? ' if pipeline_id else '')+'ORDER BY started_at', (pipeline_id,) if pipeline_id else ()).fetchall()
    known=[r for r in rows if r['input_tokens'] is not None]
    total=sum(r['input_tokens'] for r in known);cached=sum(r['cached_input_tokens'] or 0 for r in known)
    return {'requests':len(rows),'measured_requests':len(known),'missing_usage_requests':len(rows)-len(known),
            'input_tokens':total,'cached_input_tokens':cached,'uncached_input_tokens':total-cached,
            'output_tokens':sum(r['output_tokens'] or 0 for r in known),'cache_hit_fraction':cached/total if total else None,
            'peak_cost_estimate_usd':(cached*.006+(total-cached)*.30+sum(r['output_tokens'] or 0 for r in known)*1.20)/1e6,
            'recent':[{'request_id':r['request_id'],'worker_id':r['worker_id'],'run_id':r['run_id'],'state':r['state'],
                       'input_tokens':r['input_tokens'],'cached_input_tokens':r['cached_input_tokens'],
                       'metadata':json.loads(r['metadata'])} for r in rows[-12:]]}
