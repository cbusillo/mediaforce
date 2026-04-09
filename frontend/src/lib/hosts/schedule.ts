import type { HostRuntime } from '$lib/api/types';

function parseScheduleStartMinutes(detail: string): number | null {
	const match = detail.match(/(\d{2}):(\d{2})/);
	if (!match) return null;
	const hours = Number(match[1]);
	const minutes = Number(match[2]);
	if (!Number.isFinite(hours) || !Number.isFinite(minutes)) return null;
	return hours * 60 + minutes;
}

function formatMinutes(minutes: number): string {
	const hours = Math.floor(minutes / 60) % 24;
	const remainder = minutes % 60;
	return `${String(hours).padStart(2, '0')}:${String(remainder).padStart(2, '0')}`;
}

export function allQueueWorkersScheduledOffWindow(hosts: HostRuntime[]): boolean {
	const queueHosts = hosts.filter((host) => host.capabilities.includes('encode_queue'));
	if (!queueHosts.length) return false;
	return queueHosts.every(
		(host) =>
			host.available &&
			host.schedule_profile_label !== 'Never' &&
			host.schedule_open === false
	);
}

export function nextQueueWindowCopy(hosts: HostRuntime[], now = new Date()): string | null {
	const currentMinutes = now.getHours() * 60 + now.getMinutes();
	const queueHosts = hosts.filter(
		(host) =>
			host.capabilities.includes('encode_queue') &&
			host.available &&
			host.schedule_profile_label !== 'Never' &&
			host.schedule_open === false
	);

	let bestMatch: { host: HostRuntime; delta: number; startMinutes: number } | null = null;
	for (const host of queueHosts) {
		const startMinutes = parseScheduleStartMinutes(host.schedule_detail);
		if (startMinutes == null) continue;
		const delta = (startMinutes - currentMinutes + 24 * 60) % (24 * 60);
		if (!bestMatch || delta < bestMatch.delta) {
			bestMatch = { host, delta, startMinutes };
		}
	}

	if (!bestMatch) return null;
	return `${formatMinutes(bestMatch.startMinutes)} on ${bestMatch.host.label}`;
}
