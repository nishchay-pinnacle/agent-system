const {test}=require('node:test');
const assert=require('node:assert/strict');
const {readFileSync}=require('node:fs');
const {runInNewContext}=require('node:vm');
const {join}=require('node:path');
const scope={sessionStorage:{getItem:()=>null}};
runInNewContext(readFileSync(join(__dirname,'../src/agent_system/dashboard-system.js'),'utf8'),scope);
test('worker liveness requires fresh explicit online heartbeat',()=>{
 const now=Date.parse('2026-01-01T12:00:00Z');
 assert.equal(scope.systemWorkerLive({online:true,heartbeat:'2026-01-01T11:59:58Z'},now),true);
 assert.equal(scope.systemWorkerLive({online:true,heartbeat:'2026-01-01T11:59:00Z'},now),false);
 assert.equal(scope.systemWorkerLive({online:false,heartbeat:'2026-01-01T12:00:00Z'},now),false);
});
test('answer coverage is independent from completed work and dependencies remain searchable',()=>{
 const q={question_id:'S1',text:'Product scope',answer:'Known boundaries',missing:'Deployment limits',area:'scope',answer_status:'partial',work_status:'finished',importance:1,dependencies:[{fact:'Hosting region',question_ids:['C2']}]};
 assert.equal(scope.systemQuestionMatches(q,{answer:'partial',work:'finished',dependency:'C2'}),true);
 assert.equal(scope.systemQuestionMatches(q,{answer:'answered'}),false);
 assert.equal(scope.systemQuestionMatches(q,{search:'deployment',importance:'1'}),true);
});
