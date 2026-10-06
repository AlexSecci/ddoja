let token = '', paused = false, jobs = [], serverCurrent = false;
const $ = id => document.getElementById(id);
const element = (tag, text, cls) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (cls) node.className = cls; return node; };
async function api(path, options = {}) {
  const response = await fetch('/api/' + path, {...options, headers: {'Content-Type':'application/json','X-Pipeline-Token':token, ...options.headers}});
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || response.statusText);
  return data;
}
function status(text) { $('status').textContent = text; }
function renderJobs() {
  const search = $('search').value.toLowerCase();
  const rows = jobs.filter(j => [j.title,j.requisition_id,j.company_id].join(' ').toLowerCase().includes(search));
  $('job-rows').replaceChildren(...rows.map(job => {
    const row = element('tr'), title = element('td',job.title);
    title.append(element('span',`${job.company_id} · ${job.requisition_id || 'No requisition ID'}`,'sub'));
    const phase = element('td'); phase.append(element('span',job.phase.replaceAll('_',' '),'badge'));
    const action = element('td'), button = element('button','Inspect');
    button.addEventListener('click',async () => {
      try { const detail = await api('jobs/' + job.id); $('detail-title').textContent = job.title; $('detail-text').textContent = JSON.stringify(detail,null,2);
        const latest=detail.evaluations.length?JSON.parse(detail.evaluations.at(-1).payload):null;
        const summary=element('div');summary.append(element('p',`${job.location} · ${job.phase.replaceAll('_',' ')} · ${job.run_status.replaceAll('_',' ')}`),element('p',job.reason||'Awaiting evaluation.'));
        const listing=element('a','Employer listing');listing.href=job.url;listing.target='_blank';listing.rel='noopener';summary.append(listing);
        if(latest) summary.append(element('p',`Evaluation: ${latest.decision}. ${latest.reason}`),element('p',`Sponsorship: ${latest.sponsorship||'Unknown'}`));
        $('detail-summary').replaceChildren(summary);
        $('detail-packages').replaceChildren(...detail.packages.map(packageRecord=>{
          const manifest=JSON.parse(packageRecord.manifest),card=element('div',undefined,'record');card.append(element('h3',manifest.upload_ready?'Reviewed material package':'Draft material package'),element('p',manifest.review_note));
          for(const item of manifest.rendered||[]){const filename=item.pdf.replaceAll('\\','/').split('/').at(-1),button=element('button',filename==='resume.pdf'?'Download résumé PDF':'Download cover letter PDF');
            button.addEventListener('click',async()=>{try{const response=await fetch(`/api/packages/${manifest.package_id}/${filename}`,{headers:{'X-Pipeline-Token':token}});if(!response.ok)throw new Error((await response.json()).detail);const url=URL.createObjectURL(await response.blob()),link=element('a');link.href=url;link.download=filename;link.click();URL.revokeObjectURL(url);}catch(error){status(error.message);}});card.append(button);}
          if(manifest.review?.issues?.length)card.append(element('p',manifest.review.issues.join(' ')));return card;
        }));
        $('detail').classList.remove('hidden'); $('detail').scrollIntoView({behavior:'smooth'}); }
      catch(error) { status(error.message); }
    });
    action.append(button); row.append(title,element('td',job.location),phase,element('td',job.run_status.replaceAll('_',' ')),action); return row;
  }));
}
async function refresh() {
  const [summary, jobData, questions, technologies, companies] = await Promise.all(['summary','jobs','questions','technologies','companies'].map(path=>api(path)));
  jobs = jobData; paused = summary.paused; $('pause').textContent = paused ? 'Resume queue' : 'Pause queue';
  $('integration-notice').textContent = serverCurrent ? `Offline pipeline · Subscription workers verified · ${summary.browser?.detail || 'Browser test pending'} · Live submission disabled` : 'Pipeline update saved. Restart the local service to enable feeds, batch processing and PDF review.';
  for (const id of ['evaluate','materials','review']) $(id).disabled=!serverCurrent;
  $('batch-form').querySelector('button').disabled=!serverCurrent;
  $('company-form').querySelector('button').disabled=!serverCurrent;
  const cards = [[summary.job_phases.submitted || 0,'Prior confirmed submissions'],[jobs.filter(j=>!['submitted','discarded','closed'].includes(j.phase)).length,'Jobs in progress'],[technologies.length,'Saved technology answers'],[summary.pending_questions,'Pending questions']];
  $('summary').replaceChildren(...cards.map(([count,label])=>{const card=element('div',undefined,'card');card.append(element('strong',count),element('span',label));return card;}));
  renderJobs();
  const pending = questions.filter(q=>q.status==='pending');
  $('question-list').replaceChildren(...(pending.length ? pending.map(question=>{
    const card=element('div',undefined,'record'); card.append(element('h3',question.prompt));
    const form=element('form',undefined,'question-form'),select=element('select'),input=element('textarea');
    for(const [value,label] of [['','Choose'],['yes','Yes'],['no','No']]) { const option=element('option',label);option.value=value;select.append(option); }
    select.required=true;input.required=true;input.placeholder='Brief experience context; for No, “Never used” is enough.';
    const save=element('button','Save answer');save.type='submit';form.append(select,input,save);
    form.addEventListener('submit',async event=>{event.preventDefault();save.disabled=true;try{await api(`questions/${question.id}/answer`,{method:'POST',body:JSON.stringify({answer:select.value,context:input.value})});await refresh();status('Answer saved; affected jobs will be reassessed.');}catch(error){status(error.message);save.disabled=false;}});
    card.append(form);return card;
  }) : [element('p','No pending questions.')]));
  $('technology-list').replaceChildren(...technologies.sort((a,b)=>a.name.localeCompare(b.name)).map(tech=>{
    const card=element('div',undefined,'record');card.append(element('h3',`${tech.name} · ${tech.answer.toUpperCase()}`),element('p',tech.context),element('span',tech.source,'sub'));return card;
  }));
  $('company-list').replaceChildren(...companies.map(company=>{
    const card=element('div',undefined,'record');card.append(element('h3',company.name),element('p',company.enabled?'Enabled':'Disabled'),element('span',company.careers_url,'sub'));
    if (company.source) card.append(element('p',`${company.source.adapter} · ${company.source.board_token}`));
    if (company.scan_note) card.append(element('p',company.scan_note));
    const scan=element('button','Scan published listings');scan.disabled=!serverCurrent||!company.enabled||!company.source;
    scan.addEventListener('click',async()=>{try{const run=await api(`companies/${company.id}/scan`,{method:'POST'});status('Company scan started.');pollRun(run.run_id,'discovery');}catch(error){status(error.message);}});
    const toggle=element('button',company.enabled?'Disable company':'Enable company');toggle.disabled=!serverCurrent||company.id==='tesla';
    toggle.addEventListener('click',async()=>{try{await api('companies',{method:'POST',body:JSON.stringify({id:company.id,name:company.name,careers_url:company.careers_url,enabled:!company.enabled})});await refresh();}catch(error){status(error.message);}});
    card.append(scan,toggle);return card;
  }));
}
function pollRun(runId, stage) {
  const poll=setInterval(async()=>{try{const result=await api('runs/'+runId);if(result.status!=='running'){clearInterval(poll);await refresh();status(stage==='discovery'?`${result.status}: ${result.detail||result.reason||''}`:result.status==='completed'?`${stage} completed.`:result.status==='no_work'?'No eligible queued work in enabled companies.':`Worker failed: ${result.reason}`);}}catch(error){clearInterval(poll);status(error.message);}},3000);
}
async function startWorker(stage) {
  try {
    const run=await api('work/'+stage,{method:'POST'});status(`${stage} worker started. Uses your Codex subscription.`);
    pollRun(run.run_id,stage);
  } catch(error) { status(error.message); }
}
document.querySelectorAll('nav button').forEach(button=>button.addEventListener('click',()=>{document.querySelectorAll('.view').forEach(view=>view.classList.toggle('hidden',view.id!==button.dataset.view));document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('active',b===button));}));
$('search').addEventListener('input',renderJobs);
$('close-detail').addEventListener('click',()=>$('detail').classList.add('hidden'));
$('refresh').addEventListener('click',()=>refresh().catch(error=>status(error.message)));
$('pause').addEventListener('click',async()=>{try{await api('pause',{method:'POST',body:JSON.stringify({paused:!paused})});await refresh();status(paused?'Queue paused. Active non-browser workers may finish.':'Queue resumed.');}catch(error){status(error.message);}});
$('evaluate').addEventListener('click',()=>startWorker('evaluation'));
$('materials').addEventListener('click',()=>startWorker('materials'));
$('review').addEventListener('click',()=>startWorker('review'));
$('batch-form').addEventListener('submit',async event=>{event.preventDefault();try{const run=await api('batch',{method:'POST',body:JSON.stringify({max_tasks:Number($('batch-limit').value)})});status('Offline queue run started.');pollRun(run.run_id,'offline batch');}catch(error){status(error.message);}});
$('company-form').addEventListener('submit',async event=>{event.preventDefault();try{
  const id=$('company-id').value;
  await api('companies',{method:'POST',body:JSON.stringify({id,name:$('company-name').value,careers_url:$('company-url').value,enabled:true})});
  await api(`companies/${id}/source`,{method:'POST',body:JSON.stringify({adapter:$('company-adapter').value,board_token:$('company-token').value})});
  await refresh();status('Company source saved. Use Scan published listings when ready.');
}catch(error){status(error.message);}});
api('session').then(async session=>{token=session.token;serverCurrent=(await api('health')).release==='offline_pipeline';return refresh();}).catch(error=>status(error.message));
