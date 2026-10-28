"""Immutable, role-specific instructions and tool schemas for one worker."""
import hashlib
import json
from pathlib import Path
import re

ROLE_PROMPTS = {'discovery':'discovery_agent.md','evaluator':'evaluation_agent.md',
                'submission':'submission_agent.md','document':'document_agent.md'}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def capture(root, worker, settings):
    root = Path(root)
    backend = worker.get('context', {}).get('execution', {}).get('browser_backend', 'cua')
    compact = settings['compact_worker_context'] and backend != 'cua'
    core = 'agent_core_compact.md' if compact else 'agent_core.md'
    role = ROLE_PROMPTS[worker['role']]
    if compact and backend == 'playwright':
        role = {'discovery':'discovery_playwright.md','submission':'submission_playwright.md'}.get(worker['role'], role)
    paths = {'core': core, 'role': role}
    if backend == 'playwright':
        paths['adapter'] = 'browser_playwright.md'
    from tools.agent_mcp import ROLE_TOOLS, TOOL_DEFINITIONS
    value = {'schema':1,'role':worker['role'],'backend':backend,'compact':compact,
             'features':settings,'prompts':{k:((root/'prompts'/p).read_text(encoding='utf-8') if (root/'prompts'/p).is_file() else '') for k,p in paths.items()},
             'source_files':list(paths.values()),
             'agent_tools':[{'name':n, **TOOL_DEFINITIONS[n]} for n in ROLE_TOOLS[worker['role']]]}
    value['id'] = hashlib.sha256(encoded(value).encode()).hexdigest()
    folder = root/'data/worker-contracts'/worker['id'];folder.mkdir(parents=True, exist_ok=True)
    path = folder/(value['id']+'.json')
    if not path.exists():
        temporary = path.with_suffix('.tmp');temporary.write_text(encoded(value), encoding='utf-8');temporary.replace(path)
    return value


def load(root, worker_id, contract_id):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', worker_id) or not re.fullmatch(r'[a-f0-9]{64}', contract_id):
        raise ValueError('Invalid worker contract identity.')
    path = Path(root)/'data/worker-contracts'/worker_id/(contract_id+'.json')
    value = json.loads(path.read_text(encoding='utf-8'))
    body = {k:v for k,v in value.items() if k != 'id'}
    if value.get('id') != contract_id or hashlib.sha256(encoded(body).encode()).hexdigest() != contract_id:
        raise ValueError('Pinned worker contract changed on disk.')
    return value
