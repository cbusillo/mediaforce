/** A current outage warning from `GET /api/recovery-alerts`; `host_key` names a computer to retry. */
export type RecoveryAlert = { id: string; title: string; detail: string; host_key?: string };

/**
 * The warnings to show after a refresh: exactly the ones the server still reports, so a
 * recovered outage disappears. A failed refresh keeps the last warnings rather than hiding them.
 */
export async function refreshedRecoveryAlerts(
	current: RecoveryAlert[],
	load: () => Promise<{ alerts: RecoveryAlert[] }>
): Promise<RecoveryAlert[]> {
	try {
		return (await load()).alerts;
	} catch {
		return current;
	}
}
