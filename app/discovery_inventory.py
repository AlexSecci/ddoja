"""Durable native-observation inventory; agents, not title rules, select roles."""
import re
import uuid
from urllib.parse import urlsplit, urljoin


def observed_listing_url(text, url):
    """Resolve native relative hrefs against observed page/navigation roots."""
    if url in text:
        return True
    page = re.search(r'(?m)^- Page URL: (\S+)', text)
    if not page:
        return False
    origin = urlsplit(page[1]); target = urlsplit(url)
    if (target.scheme, target.netloc) != (origin.scheme, origin.netloc):
        return False
    hrefs = re.findall(r'(?m)^\s*- /url: (\S+)\s*$', text)
    # Some SPAs use an application-root <base>. Native breadcrumb links expose
    # that root even when the snapshot retains unexpanded href attributes.
    bases = [page[1], *[urljoin(page[1], href) for href in hrefs
             if href.startswith('/') and not href.startswith('//') and href.endswith('/')
             and origin.path.startswith(href)]]
    return any(urljoin(base, href) == url for base in bases for href in hrefs
               if not urlsplit(href).scheme and not urlsplit(href).netloc)


def native_text(payload):
    transport=payload.get('transport_event')
    item=transport.get('item') if isinstance(transport,dict) else None
    result=item.get('result') if isinstance(item,dict) else None
    content=result.get('content') if isinstance(result,dict) else None
    if not isinstance(content,list):return ''
    return '\n'.join(b['text'] for b in content if isinstance(b,dict) and isinstance(b.get('text'),str))


def saved_board_rows(text):
    """Recover identities from saved Greenhouse UI evidence, never classifications."""
    page=re.search(r'(?m)^- Page URL: (\S+)',text)
    if not page:return []
    board=urlsplit(page[1])
    if board.hostname!='job-boards.greenhouse.io' or '/jobs/' in board.path:return []
    rows=[];lines=text.splitlines()
    for n,line in enumerate(lines):
        match=re.search(r'- /url: (https://job-boards\.greenhouse\.io/[^/\s]+/jobs/(\d+))\s*$',line)
        if not match:continue
        if urlsplit(match[1]).path.split('/')[1]!=board.path.strip('/'):continue
        link_indent=len(line)-len(line.lstrip());paragraphs=[]
        for following in lines[n+1:]:
            if following.strip() and len(following)-len(following.lstrip())<link_indent:break
            paragraph=re.search(r'- paragraph(?: \[[^\n]+?\])?: (.+)$',following)
            if paragraph:paragraphs.append(paragraph[1])
        # Some native browser snapshots flatten a link's children into its
        # accessible label. Preserve that exact label without guessing where
        # the job title ends and the location starts.
        label=re.search(r'- link "(.+)"(?: \[[^\n]*\])?:?$',lines[n-1]) if n else None
        title=paragraphs[0] if paragraphs else label[1] if label else None
        if not title:continue
        rows.append({'url':match[1],'requisition_id':match[2],'title':title,
                     'location':paragraphs[1] if len(paragraphs)>1 else '', 'source_page':page[1]})
    return rows


def upsert(db,pipeline_id,entry,*,worker_id=None,event_id=None,observed_at,now,job_id=None,selected=None):
    old=db.execute('SELECT * FROM agent_discovery_inventory WHERE pipeline_id=? AND '
        '(url=? OR (? IS NOT NULL AND requisition_id=?)) ORDER BY rowid LIMIT 1',
        (pipeline_id,entry['url'],entry.get('requisition_id'),entry.get('requisition_id'))).fetchone()
    decision=('selected' if selected else 'excluded') if selected is not None else entry.get('disposition','unreviewed')
    reason=str(entry.get('reason') or '')[:4000]
    decision_source='registered_jd' if selected is not None else entry.get('decision_source','agent')
    if old and old['job_id'] and job_id is None:
        if decision=='excluded':raise ValueError('A full JD already exists; change its selection through record_listing.')
        decision=old['disposition'];reason=old['reason'];job_id=old['job_id'];decision_source=old['decision_source']
    elif (old and decision=='unreviewed' and old['worker_id']==worker_id and old['disposition']=='excluded'):
        decision=old['disposition'];reason=old['reason'];decision_source=old['decision_source']
    identity=old['id'] if old else uuid.uuid4().hex
    db.execute('INSERT INTO agent_discovery_inventory(id,pipeline_id,requisition_id,url,title,location,disposition,reason,decision_source,'
        'job_id,worker_id,source_event_id,source_page,observed_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
        'ON CONFLICT(id) DO UPDATE SET requisition_id=COALESCE(excluded.requisition_id,agent_discovery_inventory.requisition_id),'
        'title=excluded.title,location=excluded.location,disposition=excluded.disposition,reason=excluded.reason,decision_source=excluded.decision_source,'
        'job_id=COALESCE(excluded.job_id,agent_discovery_inventory.job_id),'
        'worker_id=COALESCE(excluded.worker_id,agent_discovery_inventory.worker_id),'
        'source_event_id=COALESCE(excluded.source_event_id,agent_discovery_inventory.source_event_id),'
        'source_page=CASE WHEN excluded.source_page!=\'\' THEN excluded.source_page ELSE agent_discovery_inventory.source_page END,'
        'observed_at=CASE WHEN excluded.source_event_id IS NOT NULL THEN excluded.observed_at ELSE agent_discovery_inventory.observed_at END,'
        'updated_at=excluded.updated_at',
        (identity,pipeline_id,entry.get('requisition_id'),entry['url'],entry['title'],entry.get('location') or '',
         decision,reason,decision_source,job_id,worker_id,event_id,entry.get('source_page') or '',observed_at,now))
    return identity


def link_registered_jobs(db,pipeline_id,now):
    for r in db.execute('SELECT j.*,pj.selected,pj.excluded_reason FROM pipeline_jobs pj JOIN jobs j ON j.id=pj.job_id '
                        'WHERE pj.pipeline_id=?',(pipeline_id,)).fetchall():
        upsert(db,pipeline_id,{'url':r['url'],'requisition_id':r['requisition_id'],'title':r['title'],
            'location':r['location'],'reason':r['excluded_reason']},observed_at=r['last_seen_at'],now=now,
            job_id=r['id'],selected=bool(r['selected']))


def summary(db,pipeline_id):
    rows=db.execute('SELECT disposition,job_id,worker_id,decision_source FROM agent_discovery_inventory WHERE pipeline_id=?',(pipeline_id,)).fetchall()
    jobs=db.execute('SELECT pj.job_id,pj.selected FROM pipeline_jobs pj WHERE pj.pipeline_id=?',(pipeline_id,)).fetchall()
    extra=[r for r in rows if not r['job_id']]
    return {'listing_identities':len(jobs)+len(extra),'full_jd_records':len(jobs),
        'inventory_rows':len(rows),'unreviewed_listings':sum(r['disposition']=='unreviewed' for r in extra),
        'inventory_excluded':sum(r['disposition']=='excluded' for r in extra),
        'historical_exclusions':sum(r['disposition']=='excluded' and r['decision_source']=='historical_report' for r in extra),
        'observed_native_listings':db.execute('SELECT COUNT(*) FROM agent_discovery_inventory WHERE pipeline_id=? '
            'AND source_event_id IS NOT NULL',(pipeline_id,)).fetchone()[0]}


def accounting(db,pipeline_id,coverage,worker_id=None):
    result=summary(db,pipeline_id)
    expected=coverage.get('listing_total')
    if expected is None:expected=coverage.get('enumerated_total')
    if expected is None:expected=coverage.get('published_listing_total')
    expected=expected if type(expected) is int and expected>=0 else None
    if worker_id:
        current=db.execute('SELECT disposition,job_id,decision_source FROM agent_discovery_inventory WHERE pipeline_id=? AND worker_id=?',
                           (pipeline_id,worker_id)).fetchall()
        observed=len(current)
        pending=sum(r['disposition']=='unreviewed' and not r['job_id'] for r in current)
        historical=sum(r['disposition']=='excluded' and r['decision_source']=='historical_report' for r in current)
    else:
        observed=result['listing_identities'];pending=result['unreviewed_listings'];historical=result['historical_exclusions']
    missing=max(0,expected-observed) if expected is not None else 0
    result.update(reported_listing_total=expected,pass_observed_listings=observed,
                  pass_unreviewed_listings=pending,pass_historical_exclusions=historical,
                  unaccounted_listings=missing,coverage_accounted=not missing and not pending,
                  coverage_verified=not missing and not pending and not historical)
    return result


def capture_page(db,pipeline_id,worker_id,event_id,payload,now):
    text=native_text(payload);entries=saved_board_rows(text)
    for entry in entries:
        entry['decision_source']='observation'
        upsert(db,pipeline_id,entry,worker_id=worker_id,event_id=event_id,observed_at=now,now=now)
    return len(entries)
