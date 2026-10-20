import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';

import { refreshedRecoveryAlerts, type RecoveryAlert } from './recovery-alerts';
import ShellWarnings from './ShellWarnings.svelte';

// The warning `GET /api/recovery-alerts` returns while one of two computers has lost its storage.
const outage: RecoveryAlert = {
	id: 'host:user@worker:2026-10-16T12:00:30+00:00',
	title: 'Worker is unavailable',
	detail:
		"Storage missing. Mediaforce is retrying automatically. Check this computer's network and storage if it stays unavailable. 1 other computer is still working.",
	host_key: 'user@worker'
};

function banner(recoveryAlerts: RecoveryAlert[]): string {
	return render(ShellWarnings, {
		props: {
			catalogWarning: null,
			recoveryAlerts,
			retryingAlert: null,
			retryOutcome: null,
			onRetry: () => undefined
		}
	}).body;
}

describe('recovery banner', () => {
	it('shows a computer outage and clears once the server stops reporting it', async () => {
		const responses: Array<{ alerts: RecoveryAlert[] } | Error> = [
			{ alerts: [outage] },
			new Error('Mediaforce is restarting'),
			{ alerts: [outage] },
			{ alerts: [] }
		];
		const load = async () => {
			const next = responses.shift();
			if (next === undefined || next instanceof Error) throw next;
			return next;
		};

		let alerts: RecoveryAlert[] = [];
		expect(banner(alerts)).not.toContain('role="alert"');

		alerts = await refreshedRecoveryAlerts(alerts, load);
		let html = banner(alerts);
		expect(html.match(/role="alert"/g)).toHaveLength(1);
		expect(html).toContain('Worker is unavailable');
		expect(html).toContain('1 other computer is still working.');
		expect(html).toContain('Retry connection');
		expect(html).toContain('View computers');

		// A refresh that fails keeps the warning instead of hiding an outage that may continue.
		alerts = await refreshedRecoveryAlerts(alerts, load);
		expect(banner(alerts)).toContain('Worker is unavailable');

		// Each poll shows the same incident once, not another copy.
		alerts = await refreshedRecoveryAlerts(alerts, load);
		expect(banner(alerts).match(/role="alert"/g)).toHaveLength(1);

		// Worker recovered: its fresh check cleared the warning on the server, so the banner goes.
		alerts = await refreshedRecoveryAlerts(alerts, load);
		html = banner(alerts);
		expect(html).not.toContain('role="alert"');
		expect(html).not.toContain('Worker is unavailable');
		expect(html).not.toContain('Retry connection');
	});
});
