'use strict';
function systemWorkerLive(worker, now = Date.now()) {
  return Boolean(worker.online && now - Date.parse(worker.heartbeat) < 5000);
}
function systemQuestionMatches(q, filters) {
  return (!filters.search || [q.question_id,q.text,q.answer,q.missing].join(' ').toLowerCase().includes(filters.search.toLowerCase())) &&
    (!filters.area || q.area === filters.area) && (!filters.answer || q.answer_status === filters.answer) &&
    (!filters.work || q.work_status === filters.work) && (!filters.importance || String(q.importance) === filters.importance) &&
    (!filters.dependency || q.dependencies.some(d => [d.fact,...d.question_ids].join(' ').toLowerCase().includes(filters.dependency.toLowerCase())));
}
let systemReviewPreview=null,systemReviewBatch='',systemDrafts={};
let systemData = null, systemFirst = true, systemSelected = sessionStorage.getItem('systemSelected') || 'orientation';
let systemSignature = '', systemOpen = new Set(JSON.parse(sessionStorage.getItem('systemOpen') || '[]')), systemClosed = new Set(JSON.parse(sessionStorage.getItem('systemClosed') || '[]'));
const systemFilters = JSON.parse(sessionStorage.getItem('systemFilters') || '{}');
function systemSelect(id) {systemSelected=id;sessionStorage.setItem('systemSelected',id);systemSignature='';renderSystem();}
function systemNode(title, id, status, description) {
  const b=button(title,()=>systemSelect(id),'system-node'+(systemSelected===id?' selected':''));
  b.append(pill(status),el('small',description||'')); return b;
}
async function systemCommand(kind, request, increases={}, batch=null) {
  const command={command_id:crypto.randomUUID(),job_id:systemData.job_id,kind,expected_revision:request?request.revision:systemData.system.revision,request_id:request?request.request_id:null,increases,batch_id:batch,note:''};
  try {
    const response=await fetch('/api/jobs/'+systemData.job_id+'/commands',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':systemData.csrf_token},body:JSON.stringify(command)});
    const data=await response.json();if(!response.ok)throw Error(data.error);
    await refreshSystem();
  } catch(error) {$('error').textContent=error.message;}
}
function systemApproval(approval) {
  const box=el('div',undefined,'approval-box');box.append(el('h3',approval.action),el('p',approval.reason),el('p','Remaining question: '+approval.remaining_question),el('p','Expected value: '+approval.expected_value));
  const queued=systemData.commands.find(c=>c.command.request_id===approval.request_id&&!c.outcome);
  if(queued){box.append(el('strong',queued.command.kind==='approve'?'Approved — waiting for worker':'Decision saved — waiting for worker'));return box;}
  const inputs=[];
  for(const item of approval.items){const label=el('label',`${item.scope} · ${item.resource}: ${item.used} used + ${item.reserved} reserved / ${item.current}. Suggested increase: ${item.increase}. New limit: ${item.current+item.increase}`);const input=el('input');input.type='number';input.min='1';input.step=item.resource==='active_seconds'?'any':'1';input.value=systemDrafts[approval.request_id+':'+item.resource]??item.increase;input.oninput=()=>systemDrafts[approval.request_id+':'+item.resource]=input.value;label.append(input);box.append(label);inputs.push([item,input]);}
  box.append(button('Approve suggested increase',()=>systemCommand('approve',approval)),button('Approve entered increase',()=>{const values={};for(const [item,input] of inputs){const n=Number(input.value);if(!Number.isFinite(n)||n<=0){$('error').textContent='Enter a positive increase.';return;}values[item.scope+':'+item.resource]=n;}systemCommand('approve',approval,values);}),button('Decline; retain findings',()=>systemCommand('decline',approval)));
  return box;
}
function systemDetails(doc) {
  const panel=el('article',undefined,'panel system-detail');panel.id='systemDetail';
  panel.append(button('Close details',()=>systemSelect(''),'drawer-close'));
  const a=doc.assignments[systemSelected],q=doc.questions[systemSelected],review=doc.reviews[systemSelected];
  panel.append(el('h2',a?a.topic:q?q.question_id+' · '+q.text:review?'Quality review':systemSelected==='questions'?'Question index':systemSelected.startsWith('coordinator')?'Coordinator':'Orientation'));
  if(a){panel.append(el('p',a.objective),el('p','Completion criterion: '+a.completion_criterion),el('p','Questions: '+a.question_ids.join(', ')));a.question_ids.forEach(id=>panel.append(button(id,()=>systemSelect(id))));block(panel,'Prerequisites and assumptions',a.prerequisites);block(panel,'Findings and remaining uncertainty',a.result||{status:a.status,error:a.error});block(panel,'Usage and limits',{usage:a.usage,limits:a.limits});for(const sid of a.sources)panel.append(button('Source '+sid,()=>{view('sources');openSource(sid);}));}
  else if(q){panel.append(pill(q.answer_status),pill(q.work_status),el('p',q.answer),el('p','Missing: '+q.missing),el('p',q.unresolved_reason));block(panel,'Dependencies',q.dependencies);block(panel,'History',q.history);for(const assignment of Object.values(doc.assignments).filter(a=>a.question_ids.includes(q.question_id)))panel.append(button(assignment.topic,()=>systemSelect(assignment.assignment_id)));for(const sid of q.source_ids)panel.append(button('Source '+sid,()=>{view('sources');openSource(sid);}));}
  else if(review){block(panel,'Review scope and report',review);}
  else if(systemSelected==='questions'){panel.append(el('p','Answer coverage and work status are tracked separately. Select a question below.'));}
  else {const sessions=Object.values(doc.sessions).filter(s=>systemSelected.startsWith('coordinator')?s.role==='coordinator':s.session_id===systemSelected);block(panel,'Saved agent status',sessions.map(s=>({id:s.session_id,status:s.status,activity:s.action})));}
  const events=doc.events||[];if(events.length)block(panel,'What happened',events.filter(e=>e.session_id===systemSelected));
  const activity=systemData.activities.filter(e=>systemSelected==='coordinator'?String(e.session_id).startsWith('coordinator'):e.session_id===systemSelected);
  for(const e of activity.slice(-8)){panel.append(el('p',e.timestamp+' · '+(e.summary||e.kind)));if(e.operation_id&&['count_input','generate'].includes(e.tool))panel.append(button('Inspect exact call',()=>selectCall(e.operation_id+'_'+(e.attempt||1))));}
  panel.append(button('View call history',()=>view('calls')));
  return panel;
}
function renderSystem() {
  if(!systemData)return;
  const root=$('systemRoot'),doc=systemData.system;
  if(!doc){root.replaceChildren(el('p','System orchestration was not recorded for this historical run. Existing views remain available.'));return;}
  const live=systemWorkerLive(systemData.worker),signature=JSON.stringify([doc.revision,live,systemData.commands,systemSelected,systemFilters,[...systemClosed]]);
  if(signature===systemSignature)return;systemSignature=signature;
  const scroll=window.scrollY,detailsScroll=$('systemDetail')?.scrollTop||0;
  const header=el('div',undefined,'system-summary');
  header.append(el('h2',systemData.status+' · '+(live?'Worker online':'Worker offline or stale')),el('p',doc.stop_reason||'Research execution'));
  const active=Object.values(doc.sessions).filter(s=>s.status==='running').length;
  header.append(el('p',`${live?active:0} active agents · ${Object.values(doc.questions).filter(q=>q.answer_status==='answered').length}/${Object.keys(doc.questions).length} answered · ${Object.values(doc.questions).filter(q=>q.work_status==='blocked').length} blocked`));
  header.append(el('p',['orientation','coordinator','investigator'].map(role=>role+': '+systemData.settings[role+'_model']+'/'+systemData.settings[role+'_effort']).join(' · ')));
  const meters=el('div',undefined,'budget-grid');for(const [key,limit] of Object.entries(doc.limits)){const used=doc.usage[key]||0,reserved=doc.usage['reserved_'+key]||0;const item=el('div');item.append(el('small',key),el('div',`${Math.round(used)} used + ${Math.round(reserved)} reserved / ${limit}`));const meter=el('meter');meter.max=limit;meter.value=used+reserved;meter.setAttribute('aria-label',key);item.append(meter);meters.append(item);}header.append(meters);
  const actions=el('div');doc.approvals.filter(a=>a.status==='pending').forEach(a=>actions.append(systemApproval(a)));
  if(doc.stage!=='done')actions.append(button('Finish run with current findings',()=>systemCommand('finish',null)));
  if(doc.stage==='done'&&doc.batches.length){const selector=el('select');selector.setAttribute('aria-label','Quality review batch');selector.append(new Option('Default: earliest highest-priority completed batch',''));for(const b of doc.batches)selector.append(new Option(b.batch_id,b.batch_id));selector.value=systemReviewBatch;selector.onchange=()=>{systemReviewBatch=selector.value;systemReviewPreview=null;};actions.append(selector,button('Preview Sol quality check',async()=>{try{const preview=await api('/api/quality-preview'+(selector.value?'?batch='+encodeURIComponent(selector.value):''));systemReviewPreview=preview;systemSignature='';renderSystem();}catch(e){$('error').textContent=e.message;}}));}
  if(systemReviewPreview){const box=el('div',undefined,'approval-box');block(box,'Frozen review scope and planned allowance',systemReviewPreview,true);box.append(button('Run Sol quality check',()=>systemCommand('quality',null,{},systemReviewPreview.batch_id)));actions.append(box);}
  const layout=el('div',undefined,'system-layout'),map=el('div',undefined,'system-map');
  if(doc.sessions.orientation)map.append(systemNode('Orientation','orientation',doc.sessions.orientation.status,'Complete overview pages → provisional profile'));
  const coordinators=Object.values(doc.sessions).filter(s=>s.role==='coordinator');if(coordinators.length)map.append(el('div','↓ orientation handoff','connector'),systemNode('Coordinator','coordinator',coordinators.at(-1).status,'Canonical updates and validated assignments'));
  map.append(systemNode('Question index','questions','saved',Object.keys(doc.questions).length+' stable questions'));
  for(const batch of doc.batches){const group=el('details',undefined,'batch');group.open=systemOpen.has(batch.batch_id)||(!systemClosed.has(batch.batch_id)&&!batch.incorporated);group.append(el('summary',batch.batch_id+' · '+(batch.incorporated?'incorporated':'assignments / returned results')));group.addEventListener('toggle',()=>{if(group.open){systemClosed.delete(batch.batch_id);systemOpen.add(batch.batch_id);}else{systemClosed.add(batch.batch_id);systemOpen.delete(batch.batch_id);}sessionStorage.setItem('systemClosed',JSON.stringify([...systemClosed]));sessionStorage.setItem('systemOpen',JSON.stringify([...systemOpen]));});group.append(el('p','↓ assignments · ↑ returned findings','connector'));const cards=el('div',undefined,'agent-cards');for(const id of batch.assignment_ids){const a=doc.assignments[id];const session=doc.sessions[id];const activity=session?.action?.reason||systemData.activities.filter(e=>e.session_id===id).at(-1)?.summary||'Awaiting launch';const card=systemNode(a.topic,id,a.status,a.question_ids.join(', ')+' · '+a.sources.length+' sources · '+(a.usage.generation_requests||0)+' generations');card.append(el('small',activity));if(a.error)card.append(pill('error'));if(doc.approvals.some(r=>r.session_id===id&&r.status==='pending'))card.append(pill('approval needed'));if(live&&a.status==='running'&&systemData.worker.active)card.classList.add('executing');cards.append(card);}group.append(cards);map.append(group);}
  for(const review of Object.values(doc.reviews))map.append(systemNode('Sol quality review',review.review_id,review.status,'Separate frozen evidence review'));
  layout.append(map);if(systemSelected)layout.append(systemDetails(doc));
  const questions=el('div',undefined,'panel');questions.append(el('h2','Question index'));
  const filters=el('div',undefined,'toolbar');for(const [key,options] of Object.entries({area:[...new Set(Object.values(doc.questions).map(q=>q.area))],importance:['1','2','3','4','5'],answer:['unassessed','partial','answered','not_applicable'],work:['pending','assigned','blocked','finished','exhausted']})){const select=el('select');select.setAttribute('aria-label',key);select.append(new Option('All '+key,''));options.forEach(o=>select.append(new Option(o,o)));select.value=systemFilters[key]||'';select.onchange=()=>{systemFilters[key]=select.value;sessionStorage.setItem('systemFilters',JSON.stringify(systemFilters));systemSignature='';renderSystem();};filters.append(select);}
  for(const key of ['search','dependency']){const input=el('input');input.type='search';input.placeholder=key==='search'?'Search questions':'Dependency fact or ID';input.value=systemFilters[key]||'';input.setAttribute('aria-label',key);input.onchange=()=>{systemFilters[key]=input.value;sessionStorage.setItem('systemFilters',JSON.stringify(systemFilters));systemSignature='';renderSystem();};filters.append(input);}questions.append(filters);
  for(const q of Object.values(doc.questions).filter(q=>systemQuestionMatches(q,systemFilters))){const row=button(q.question_id+(q.origin?' · added':'')+' · '+q.text,()=>systemSelect(q.question_id),'question-row');row.append(pill(q.answer_status),pill(q.work_status));questions.append(row);}
  root.replaceChildren(header,actions,layout,questions);if($('systemDetail'))$('systemDetail').scrollTop=detailsScroll;window.scrollTo(0,scroll);
}
async function refreshSystem(){
  systemData=await api('/api/system');systemData.activities=[];
  // Existing event pagination remains authoritative; request a bounded recent window.
  const events=await api('/api/events?after='+Math.max(0,sequence-100));systemData.activities=events.events;
  if(systemFirst){systemFirst=false;const remembered=sessionStorage.getItem('workbenchTab');if(remembered)view(remembered);else if(systemData.system&&systemData.system.stage!=='done')view('system');}
  renderSystem();
}
