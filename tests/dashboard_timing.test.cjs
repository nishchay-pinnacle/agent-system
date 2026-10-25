const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {runInNewContext} = require('node:vm');
const {join} = require('node:path');
const scope = {};
runInNewContext(readFileSync(join(__dirname, '../src/agent_system/dashboard-timing.js'), 'utf8'), scope);
const started = Date.parse('2026-01-01T00:00:00Z');
const pending = {started_at:'2026-01-01T00:00:00Z', ended_at:null, status:'unknown', timeout_seconds:300, configured_timeout_seconds:300};

test('pending timing advances without changing the saved record', () => {
    assert.equal(scope.callTiming(pending, started+42000), '42s since dispatch · 300s timeout · awaiting recorded outcome');
    assert.equal(scope.callTiming(pending, started+44000), '44s since dispatch · 300s timeout · awaiting recorded outcome');
    assert.equal(pending.ended_at, null);
});
test('remaining budget cap and overdue unknown outcome are explicit', () => {
    const capped = {...pending, timeout_seconds:30};
    assert.match(scope.callTiming(capped, started+25000), /25s since dispatch · 30s timeout \(configured 300s; capped by remaining budget\)/);
    assert.match(scope.callTiming(capped, started+40000), /timeout elapsed; outcome unknown$/);
});
test('completed durations stay fixed; legacy and missing fields remain honest', () => {
    assert.equal(scope.callTiming({...pending, duration:90.04, ended_at:'2026-01-01T00:01:30Z'}, started+999000), '90.04s elapsed · 300s timeout');
    assert.equal(scope.callTiming({started_at:pending.started_at,timeout_seconds:90},started+10000), '10s since dispatch · 90s timeout · awaiting recorded outcome');
    assert.equal(scope.callTiming({}, started), 'Elapsed time unavailable · timeout not recorded');
    assert.match(scope.callTiming(pending, started-1000), /^0s since dispatch/);
});
