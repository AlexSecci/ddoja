"""Host-observed tool latency; never call the residual pure model inference."""
from datetime import datetime, timezone
import threading
import time
import uuid
import math


def union_seconds(intervals):
    merged = []
    for start, end in sorted(intervals):
        if end < start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    return sum(end-start for start, end in merged)


def summarize_timings(records):
    runs = {}
    fields = ('worker_elapsed_seconds', 'startup_seconds', 'browser_tool_seconds', 'other_tool_seconds',
              'all_tool_seconds', 'tool_overlap_seconds', 'model_and_transport_seconds')
    for item in records:
        if (isinstance(item, dict) and item.get('schema') == 'worker-timing-v1' and item.get('run_id')
                and all(type(item.get(k)) in (int, float) and math.isfinite(item[k]) and item[k] >= 0 for k in fields)):
            runs[item['run_id']] = item
    result = {'measured_turns': len(runs), 'pure_inference_seconds': None,
              'status': 'measured' if runs else 'unavailable',
              **{k: sum(r[k] for r in runs.values()) if runs else None for k in fields},
              'incomplete_calls': sum(r.get('incomplete_calls', 0) for r in runs.values()),
              'unmatched_completions': sum(r.get('unmatched_completions', 0) for r in runs.values())}
    durations = sorted(c['duration_seconds'] for r in runs.values() for c in r.get('calls', [])
                       if c['category'] == 'browser' and c['complete'])
    result['browser_calls'] = len(durations)
    result['browser_mean_seconds'] = sum(durations)/len(durations) if durations else None
    result['browser_p50_seconds'] = durations[math.ceil(.5*len(durations))-1] if durations else None
    result['browser_p95_seconds'] = durations[math.ceil(.95*len(durations))-1] if durations else None
    result['notes'] = ['New instrumented turns only; historical timing is unavailable.',
        'Values sum worker time across concurrent turns, not elapsed campaign wall time.',
        'Browser and other-tool categories may overlap; all_tool_seconds is their union.',
        'Model/transport includes inference, provider queueing, network and local orchestration; pure inference is unavailable.',
        'Incomplete tool calls are censored at interruption and excluded from per-call latency statistics.']
    return result


class RunTiming:
    def __init__(self, run_id=None, *, clock=time.monotonic):
        self.run_id = run_id or uuid.uuid4().hex
        self.clock = clock
        self.started = clock()
        self.started_at_utc = datetime.now(timezone.utc).isoformat()
        self.model_started = None
        self.pending = {}
        self.calls = []
        self.orphans = 0
        self.lock = threading.RLock()

    def model_start(self):
        with self.lock:
            if self.model_started is None:
                self.model_started = self.clock()

    def tool_start(self, call_id, tool, category):
        with self.lock:
            if call_id not in self.pending and not any(c['id'] == call_id for c in self.calls):
                self.pending[call_id] = (self.clock(), tool, category)

    def tool_end(self, call_id, success=True):
        with self.lock:
            prior = self.pending.pop(call_id, None)
            if prior is None:
                if not any(c['id'] == call_id for c in self.calls):
                    self.orphans += 1
                return
            start, tool, category = prior
            self.calls.append({'id': call_id, 'tool': tool, 'category': category,
                               'start': start, 'end': self.clock(), 'success': success, 'complete': True})

    def observe(self, event):
        if event.get('type') == 'turn.started':
            self.model_start()
        item = event.get('item') or {}
        if item.get('type') != 'mcp_tool_call' or not item.get('id'):
            return
        key = str(item['id'])
        if event.get('type') == 'item.started':
            category = 'browser' if item.get('server') == 'cua_repl' else 'other'
            self.tool_start(key, str(item.get('server', ''))+'.'+str(item.get('tool', '')), category)
        elif event.get('type') == 'item.completed':
            result = item.get('result')
            self.tool_end(key, not (item.get('error') or (isinstance(result, dict) and result.get('isError'))
                                   or item.get('status') in ('failed', 'error')))

    def summary(self):
        with self.lock:
            now = self.clock()
            calls = list(self.calls) + [dict(id=key, tool=tool, category=category, start=start,
                                           end=now, success=None, complete=False)
                for key, (start, tool, category) in self.pending.items()]
            model_start = self.model_started if self.model_started is not None else now
            intervals = [(max(c['start'], model_start), c['end']) for c in calls]
            all_tools = union_seconds(intervals)
            browser = union_seconds([(max(c['start'], model_start), c['end']) for c in calls if c['category']=='browser'])
            other = union_seconds([(max(c['start'], model_start), c['end']) for c in calls if c['category']=='other'])
            active = max(0.0, now-model_start)
            return {'schema': 'worker-timing-v1', 'run_id': self.run_id, 'started_at_utc': self.started_at_utc,
                'worker_elapsed_seconds': now-self.started, 'startup_seconds': model_start-self.started,
                'browser_tool_seconds': browser, 'other_tool_seconds': other,
                'all_tool_seconds': all_tools, 'tool_overlap_seconds': browser+other-all_tools,
                'model_and_transport_seconds': max(0.0, active-all_tools), 'pure_inference_seconds': None,
                'unmatched_completions': self.orphans,
                'incomplete_calls': sum(not c['complete'] for c in calls),
                'calls': [{**{k: c[k] for k in ('id','tool','category','success','complete')},
                           'start_seconds': c['start']-self.started, 'end_seconds': c['end']-self.started,
                           'duration_seconds': max(0.0,c['end']-c['start'])} for c in calls],
                'notes': 'Host-observed latency. Browser includes driver/transport/site waits. Residual includes model, network and orchestration, not pure inference. Tool unions merge parallel calls within a worker.'}
