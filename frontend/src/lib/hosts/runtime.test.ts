import { describe, expect, it } from 'vitest';

import type { HostRuntime, HostsPayload } from '$lib/api/types';
import { HOST_STATUS_PENDING_MESSAGE, hostRuntimeBadgeState, hostsStatusPending } from './runtime';

function runtime(overrides: Partial<HostRuntime> = {}): HostRuntime {
	return {
		key: 'worker',
		label: 'Worker',
		priority: 1,
		capabilities: ['encode_queue'],
		available: false,
		message: 'Offline',
		missing_paths: [],
		issues: [],
		detail: null,
		max_parallel_encodes: 1,
		active_encode_count: 0,
		schedule_profile_label: 'Always',
		schedule_detail: 'runs anytime',
		active_flag: 'idle',
		active_reason: '',
		...overrides
	};
}

describe('host runtime status', () => {
	it('shows startup probes as checking instead of offline', () => {
		const pending = runtime({ message: HOST_STATUS_PENDING_MESSAGE });

		expect(hostRuntimeBadgeState(pending)).toEqual({ tone: 'wait', label: 'Checking' });
		expect(hostsStatusPending({ compact: true, hosts: [pending] } satisfies HostsPayload)).toBe(
			true
		);
	});

	it('stops treating hosts as pending once a real result arrives', () => {
		const ready = runtime({ available: true, message: 'Mounted and ready' });
		const reachable = runtime({
			probe_available: true,
			storage_recovery_available: true,
			message: 'Storage will reconnect when work starts'
		});
		const needsSetup = runtime({ probe_available: true, issues: ['Install ffmpeg first'] });
		const offline = runtime({ probe_available: false, message: 'Turn on SSH first' });

		expect(hostRuntimeBadgeState(ready)).toEqual({ tone: 'ready', label: 'Ready' });
		expect(hostRuntimeBadgeState(reachable)).toEqual({ tone: 'wait', label: 'Reachable' });
		expect(hostRuntimeBadgeState(needsSetup)).toEqual({ tone: 'wait', label: 'Needs setup' });
		expect(hostRuntimeBadgeState(offline)).toEqual({ tone: 'fail', label: 'Unavailable' });
		expect(
			hostsStatusPending({ compact: true, hosts: [ready, reachable, needsSetup, offline] })
		).toBe(false);
	});
});
