"""Narrow recovery for a proven client-side reference lookup failure, not a send."""
import json
import re


def recover_unexecuted_click(store,worker_id):
    from .store import timestamp
    from .browser_sessions import BrowserSessions
    from .trial_limits import read,expired
    with store.connect(write=True) as db:
        w=db.execute('SELECT w.*,p.stop_requested,p.status AS pipeline_status FROM agent_workers w JOIN pipelines p ON p.id=w.pipeline_id WHERE w.id=?',(worker_id,)).fetchone()
        if (not w or w['role']!='submission' or w['turn_active'] or w['state']!='uncertain'
                or w['stop_requested'] or w['pipeline_status'] in {'stopped','stopping','completed'}):return False
        context=json.loads(w['context'])
        if (not context.get('final_action_may_have_been_sent') or context.get('email_verification_final_scope')
                or context.get('unexecuted_click_recovery') or not context.get('final_action_authorized_at')):return False
        job=db.execute('SELECT * FROM jobs WHERE id=?',(w['job_id'],)).fetchone()
        if (not job or job['phase']=='submitted' or job['jd_version']!=w['jd_version']
                or store.profile_version(db)!=w['profile_version']
                or db.execute("SELECT 1 FROM agent_submission_results WHERE job_id=? AND state='submitted'",(w['job_id'],)).fetchone()):return False
        trial=read(db,w['pipeline_id'])
        if trial and (trial['state']!='active' or expired(trial) or w['job_id'] not in trial['job_ids']):return False
        quota=db.execute("SELECT value FROM meta WHERE key='agent_quota_latch'").fetchone()
        if quota and json.loads(quota[0]).get('paused'):return False
        # Exactly one lifetime authorization, with exactly one attempted click.
        # Timeouts, disconnected transports, successful clicks, other mutations,
        # later verification sends and an already retried attempt are excluded.
        authorizations=db.execute("SELECT COUNT(*) FROM events WHERE kind='agent.final_action_authorized' AND json_extract(payload,'$.worker_id')=?",(worker_id,)).fetchone()[0]
        if authorizations!=1:return False
        rows=db.execute("SELECT * FROM agent_runtime_events WHERE worker_id=? AND session_id=? AND tool_name LIKE 'playwright.%' AND created_at>=? ORDER BY rowid",(worker_id,w['session_id'],context['final_action_authorized_at'])).fetchall()
        mutations=[r for r in rows if r['tool_name'] not in {'playwright.browser_snapshot','playwright.browser_take_screenshot'}]
        if len(mutations)!=1:return False
        failed=mutations[0]
        if failed['tool_name']!='playwright.browser_click' or failed['success']!=0:return False
        item=json.loads(failed['payload']).get('transport_event',{}).get('item',{})
        target=item.get('arguments',{}).get('target','')
        blocks=(item.get('result') or {}).get('content',[])
        if not re.fullmatch(r'e\d+',target) or len(blocks)!=1:return False
        expected='### Error\nError: Ref '+target+' not found in the current page snapshot. Try capturing new snapshot.'
        if item.get('status')!='failed' or blocks[0].get('text','').strip()!=expected:return False
        current=store._agent_latest_cua_event(db,w,success=True)
        if not current or current.get('application_url')!=job['url'] or (current.get('verification') or {}).get('present'):return False
        if not rows or rows[-1]['id']!=current['event_id'] or rows[-1]['tool_name']!='playwright.browser_snapshot':return False
        if BrowserSessions(store.root).inspect(worker_id)['state']!='live':return False
        context['unexecuted_click_recovery']={'source_event_id':failed['id'],'prior_authorized_at':context['final_action_authorized_at'],
            'prior_send_flag':True,'reconciled_at':timestamp(),
            'proof':'Native browser reference lookup failed before any click executed; fresh exact-page snapshot retained.',
            'instruction':'Use the fresh full snapshot and current Submit reference. No final click executed. Existing actual approval scopes still apply.'}
        context['final_action_may_have_been_sent']=False
        db.execute("UPDATE agent_workers SET state='queued',checkpoint='browser_reference_reconciled',context=?,error='',completed_at=NULL,capability_sha256='',capability_expires_at=NULL WHERE id=?",(json.dumps(context),worker_id))
        db.execute("UPDATE jobs SET run_status='idle',reason='Native browser proved the only click failed reference lookup before execution.' WHERE id=?",(w['job_id'],))
        db.execute("UPDATE pipeline_jobs SET agent_status='approved' WHERE pipeline_id=? AND job_id=?",(w['pipeline_id'],w['job_id']))
        store.event(db,'agent.unexecuted_click_reconciled',{'worker_id':worker_id,'source_event_id':failed['id'],'prior_send_history_preserved':True,'human_approvals_unchanged':True},w['job_id'],'scheduler')
        return True
