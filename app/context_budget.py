"""Host-owned model-history handoffs at pre-send boundaries; browser stays live."""
import json
from pathlib import Path


def limit(root):
    path=Path(root)/'config/submission-efficiency.json'
    value=json.loads(path.read_text()) if path.exists() else {}
    number=value.get('context_handoff_input_tokens',90000)
    return number if type(number) is int and 40000<=number<=250000 else 90000


def last_input(db,worker_id):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='agent_provider_requests'").fetchone():return 0
    row=db.execute('SELECT input_tokens FROM agent_provider_requests WHERE worker_id=? AND input_tokens IS NOT NULL ORDER BY started_at DESC LIMIT 1',(worker_id,)).fetchone()
    return row[0] if row else 0


def rotate(store,db,worker):
    context=store._agent_decode(worker['context'],{})
    if (worker['role']!='submission' or not worker['session_id']
            or store._context_may_have_sent_final(context,worker['checkpoint'])
            or context.get('execution',{}).get('browser_backend')!='playwright'):
        return worker
    if not context.get('context_handoff_pending') and last_input(db,worker['id'])<limit(store.root):return worker
    from .store import timestamp
    history=context.setdefault('context_handoffs',[])
    history.append({'session_id':worker['session_id'],'checkpoint':worker['checkpoint'],
                    'at':timestamp(),'input_tokens':last_input(db,worker['id'])})
    context['context_handoffs']=history[-4:]
    context.pop('context_handoff_pending',None)
    db.execute("UPDATE agent_workers SET session_id=NULL,resume_mode='new_session',context=? WHERE id=?",(json.dumps(context),worker['id']))
    store.event(db,'agent.safe_context_handoff',{'worker_id':worker['id'],'prior_session_id':worker['session_id'],
                'browser_preserved':True,'checkpoint_preserved':True},worker['job_id'],'scheduler')
    return {**dict(worker),**dict(db.execute('SELECT * FROM agent_workers WHERE id=?',(worker['id'],)).fetchone())}
