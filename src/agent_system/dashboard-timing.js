'use strict';

// Unknown outcomes may be live requests or interrupted workers. A clock alone
// cannot establish liveness, especially after the effective timeout has elapsed.
function callTiming(metadata, nowMs = Date.now()) {
    const effective = metadata.timeout_seconds;
    const configured = metadata.configured_timeout_seconds ?? effective;
    const timeout = Number.isFinite(effective) ? effective + 's timeout' : 'timeout not recorded';
    const configuredNote = Number.isFinite(configured) && configured !== effective
        ? ' (configured ' + configured + 's; capped by remaining budget)' : '';
    if (Number.isFinite(metadata.duration)) {
        return metadata.duration.toFixed(2) + 's elapsed · ' + timeout + configuredNote;
    }
    const start = Date.parse(metadata.started_at);
    const end = metadata.ended_at ? Date.parse(metadata.ended_at) : nowMs;
    if (!Number.isFinite(start) || !Number.isFinite(end)) {
        return 'Elapsed time unavailable · ' + timeout + configuredNote;
    }
    const elapsed = Math.max(0, (end - start) / 1000);
    const unresolved = !metadata.ended_at;
    const suffix = unresolved
        ? (Number.isFinite(effective) && elapsed >= effective
            ? ' · timeout elapsed; outcome unknown'
            : ' · awaiting recorded outcome')
        : '';
    return Math.floor(elapsed) + 's ' + (unresolved ? 'since dispatch' : 'elapsed')
        + ' · ' + timeout + configuredNote + suffix;
}
