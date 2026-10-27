// Run with node tests/dashboard_polling.cjs; no packages or browser needed.
// Exercise the actual scheduler against delayed fetches and run switches.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('src/research_agent/static/dashboard.html', 'utf8');
const script = html.slice(html.indexOf('async function fetchJSON'), html.indexOf('</script>'));
const pending = [], timers = [], renders = [];
const nodes = {
  runs: {value: 'A', options: [{value: 'A'}, {value: 'B'}]},
  events: {rows: [], append(item) { this.rows.push(item); }, replaceChildren() { this.rows = []; }},
  activity: {scrollHeight: 100, scrollTop: 0, clientHeight: 100},
  error: {textContent: ''}, filter: {},
};
const context = vm.createContext({
  run: 'A', generation: 0, offset: 0, last: '', data: {}, expanded: new Set(),
  $: id => nodes[id],
  el: (tag, text) => ({tag, text, append(...children) {this.children = children;}}),
  render: () => renders.push(vm.runInContext('data', context)),
  document: {querySelectorAll: () => []},
  fetch: url => new Promise((resolve, reject) => pending.push({url, resolve, reject})),
  AbortSignal: {timeout: () => undefined},
  setTimeout: (callback, delay) => {assert.equal(delay, 1000);timers.push(callback);},
  encodeURIComponent,
});
const flush = () => new Promise(resolve => setImmediate(resolve));
async function respond(path, value) {
  assert.equal(pending.length, 1, 'only one fetch may be in flight');
  const request = pending.shift();assert.equal(request.url, path);
  request.resolve({ok: true, json: async () => value});await flush();
}
(async () => {
  vm.runInContext(script, context);
  await respond('/api/runs', {runs: ['A', 'B']});
  assert.equal(timers.length, 0, 'no next poll while request is delayed');
  await flush();assert.equal(pending.length, 1);
  await respond('/api/state?run=A', {questions: {active_question_id: 'Q1'}});
  await respond('/api/events?run=A&offset=0', {events: [{name:'one', kind:'Tool', details:{}}], offset:100});
  assert.equal(context.offset, 100);assert.equal(nodes.events.rows.length, 1);
  assert.equal(timers.length, 1);timers.shift()();
  await respond('/api/runs', {runs:['A','B']});
  await respond('/api/state?run=A', {questions: {active_question_id: 'Q2'}});
  await respond('/api/events?run=A&offset=100', {events:[{name:'two',kind:'Tool',details:{}}], offset:200});
  assert.equal(context.offset, 200);assert.equal(nodes.events.rows.length, 2);
  assert.equal(context.data.questions.active_question_id, 'Q2');
  timers.shift()();await respond('/api/runs', {runs:['A','B']});
  // Switching away and back must invalidate the old A request as well.
  nodes.runs.value='B';nodes.runs.onchange();
  nodes.runs.value='A';nodes.runs.onchange();
  assert.equal(pending.length, 1, 'selection must not start an overlapping poll');
  await respond('/api/state?run=A', {questions: {active_question_id:'stale'}});
  assert.equal(context.offset, 0);assert.equal(pending.length, 0);
  assert.equal(context.data.questions, undefined);
  timers.shift()();await respond('/api/runs', {runs:['A','B']});
  pending.shift().reject(Error('offline'));await flush();
  assert.equal(nodes.error.textContent,'offline');assert.equal(timers.length,1);
  console.log('Dashboard polling regression passed: serialized requests, stable cursor, stale-run rejection, error recovery.');
})().catch(error => {console.error(error);process.exitCode=1;});
