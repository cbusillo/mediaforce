import type {
	EncodeQueueJob,
	EncodeQueueSummary,
	EncodeScheduleState,
	HostRuntime
} from '$lib/api/types';
import { formatScheduleCountdown, formatScheduleMoment } from '$lib/format';

export type SchedulePresentationTone = 'idle' | 'ready' | 'wait' | 'active' | 'fail';

export type SchedulePresentation = {
	state: EncodeScheduleState | 'host_open' | 'host_draining' | 'host_off_schedule';
	label: string;
	tone: SchedulePresentationTone;
	detail: string;
	transitionAt?: string | null;
};

function compactText(value: unknown): string {
	return typeof value === 'string' ? value.trim() : '';
}

function record(value: unknown): Record<string, unknown> | null {
	return value && typeof value === 'object' ? (value as Record<string, unknown>) : null;
}

function hostIdentityTokens(host: HostRuntime): Set<string> {
	return new Set(
		[host.key, host.host, host.label]
			.map((value) => compactText(value).toLowerCase())
			.filter(Boolean)
	);
}

function jobHostTokens(job: EncodeQueueJob): Set<string> {
	const hosts = [record(job.host), ...(job.active_hosts ?? []).map(record)].filter(
		(host): host is Record<string, unknown> => Boolean(host)
	);
	return new Set(
		hosts
			.flatMap((host) => [host.key, host.host, host.label])
			.map((value) => compactText(value).toLowerCase())
			.filter(Boolean)
	);
}

function jobRunsOnHost(job: EncodeQueueJob, host: HostRuntime): boolean {
	const hostTokens = hostIdentityTokens(host);
	return [...jobHostTokens(job)].some((token) => hostTokens.has(token));
}

function jobLibrary(job: EncodeQueueJob): string {
	return compactText(job.prefix).split('/')[0]?.toLowerCase() ?? '';
}

function hostAllowsJob(host: HostRuntime, job: EncodeQueueJob): boolean {
	if (!host.capabilities.includes('encode_queue')) return false;
	const allowedLibraries = host.allowed_libraries
		?.map((value) => compactText(value).toLowerCase())
		.filter(Boolean);
	return !allowedLibraries?.length || allowedLibraries.includes(jobLibrary(job));
}

function assignedHost(job: EncodeQueueJob, hosts: HostRuntime[]): HostRuntime | null {
	return hosts.find((host) => jobRunsOnHost(job, host)) ?? null;
}

function compatibleNextOpening(
	job: EncodeQueueJob,
	hosts: HostRuntime[]
): { host: HostRuntime; opensAt: string } | null {
	return (
		hosts
			.filter(
				(host) =>
					hostAllowsJob(host, job) &&
					(host.available || host.storage_recovery_available === true) &&
					host.schedule_profile_label !== 'Never' &&
					Boolean(host.schedule_next_opens_at)
			)
			.map((host) => ({ host, opensAt: compactText(host.schedule_next_opens_at) }))
			.filter((candidate) => Number.isFinite(Date.parse(candidate.opensAt)))
			.sort((left, right) => Date.parse(left.opensAt) - Date.parse(right.opensAt))[0] ?? null
	);
}

function inferredScheduleState(job: EncodeQueueJob): EncodeScheduleState {
	if (job.schedule_state) return job.schedule_state;
	const status = compactText(job.status).toLowerCase();
	if (job.bypass_schedule && ['running', 'queued'].includes(status)) return 'bypassed';
	if (status === 'running') {
		return job.schedule_close_deadline_at ? 'active_hard_stop' : 'running';
	}
	if (status !== 'queued') return 'none';
	if (job.progress?.progress_state === 'schedule_waiting') return 'schedule_interrupted';
	if (job.schedule_waiting) return 'off_schedule';
	return job.waiting_reason ? 'waiting' : 'ready';
}

function transitionCopy(
	value: string | null | undefined,
	host: HostRuntime | null,
	now: Date
): string | null {
	return formatScheduleMoment(value, host?.schedule_timezone, now);
}

export function jobSchedulePresentation(
	job: EncodeQueueJob,
	hosts: HostRuntime[],
	now = new Date()
): SchedulePresentation {
	const state = inferredScheduleState(job);
	const host = assignedHost(job, hosts);
	if (state === 'active_hard_stop') {
		const moment = transitionCopy(job.schedule_close_deadline_at, host, now);
		const countdown = formatScheduleCountdown(job.schedule_close_deadline_at, now);
		const timing = [
			moment ? `Stops ${moment}` : 'Stops when the work window closes',
			countdown ? `${countdown} remaining` : null
		]
			.filter(Boolean)
			.join(' · ');
		return {
			state,
			label: 'Stops at close',
			tone: 'active',
			detail: `${timing}. If unfinished, this item returns to the queue automatically.`,
			transitionAt: job.schedule_close_deadline_at
		};
	}
	if (state === 'bypassed') {
		return {
			state,
			label: 'Bypassing schedule',
			tone: 'wait',
			detail: 'This work ignores the normal worker close time and can continue past it.'
		};
	}
	if (state === 'schedule_interrupted') {
		const nextOpening = compatibleNextOpening(job, hosts);
		const opening = nextOpening ? transitionCopy(nextOpening.opensAt, nextOpening.host, now) : null;
		const detail =
			nextOpening && opening
				? `The interrupted item will restart from the beginning automatically when ${nextOpening.host.label} opens ${opening}. No failure attempt was used.`
				: 'The interrupted item will restart from the beginning automatically in the next compatible work window. No failure attempt was used.';
		return {
			state,
			label: 'Paused by schedule',
			tone: 'wait',
			detail,
			transitionAt: nextOpening?.opensAt
		};
	}
	if (state === 'draining_no_fit') {
		const nextOpening = compatibleNextOpening(job, hosts);
		const opening = nextOpening ? transitionCopy(nextOpening.opensAt, nextOpening.host, now) : null;
		const detail =
			nextOpening && opening
				? `No queued item safely fits the time left. It will retry automatically when ${nextOpening.host.label} opens ${opening}.`
				: 'No queued item safely fits the time left. It will retry automatically in the next compatible full work window.';
		return {
			state,
			label: 'Waiting for full window',
			tone: 'wait',
			detail,
			transitionAt: nextOpening?.opensAt
		};
	}
	if (state === 'draining_impossible') {
		return {
			state,
			label: 'Window too short',
			tone: 'fail',
			detail:
				compactText(job.waiting_reason) ||
				'The estimated item is longer than every compatible work window. Widen a worker window or use Bypass scheduler.'
		};
	}
	if (state === 'off_schedule') {
		const nextOpening = compatibleNextOpening(job, hosts);
		const opening = nextOpening ? transitionCopy(nextOpening.opensAt, nextOpening.host, now) : null;
		const detail =
			nextOpening && opening
				? `Starts automatically when ${nextOpening.host.label} opens ${opening}.`
				: 'Starts automatically when a compatible worker window opens.';
		return {
			state,
			label: 'Off schedule',
			tone: 'wait',
			detail,
			transitionAt: nextOpening?.opensAt
		};
	}
	if (state === 'running') {
		return {
			state,
			label: 'Running',
			tone: 'active',
			detail: compactText(job.scheduler_status_copy) || 'Running inside the worker work window.'
		};
	}
	if (state === 'ready') {
		return {
			state,
			label: 'Ready',
			tone: 'ready',
			detail: compactText(job.scheduler_status_copy) || 'Ready when a compatible worker is free.'
		};
	}
	return {
		state,
		label: state === 'waiting' ? 'Waiting' : 'No schedule state',
		tone: state === 'waiting' ? 'wait' : 'idle',
		detail: compactText(job.waiting_reason) || compactText(job.scheduler_status_copy) || '—'
	};
}

function hostHasDrainCandidate(
	host: HostRuntime,
	queue: EncodeQueueSummary | null | undefined
): boolean {
	if (!queue || host.schedule_open !== true || !host.schedule_closes_at) return false;
	return queue.queued.some(
		(job) =>
			hostAllowsJob(host, job) &&
			['draining_no_fit', 'draining_impossible'].includes(inferredScheduleState(job))
	);
}

function activeBypassJob(host: HostRuntime, queue: EncodeQueueSummary | null | undefined): boolean {
	return Boolean(
		queue?.running.some((job) => job.bypass_schedule === true && jobRunsOnHost(job, host))
	);
}

export function hostSchedulePresentation(
	host: HostRuntime,
	queue?: EncodeQueueSummary | null,
	now = new Date()
): SchedulePresentation | null {
	if (!host.capabilities.includes('encode_queue')) return null;
	if (activeBypassJob(host, queue)) {
		return {
			state: 'bypassed',
			label: 'Bypassing schedule',
			tone: 'wait',
			detail: 'Active work can continue after this worker’s normal close time.'
		};
	}
	if (host.schedule_profile_label === 'Never') {
		return {
			state: 'host_off_schedule',
			label: 'Not accepting',
			tone: 'idle',
			detail: 'This worker’s work window is set to Never.'
		};
	}
	if (host.schedule_open === false) {
		const opening = transitionCopy(host.schedule_next_opens_at, host, now);
		return {
			state: 'host_off_schedule',
			label: 'Off schedule',
			tone: 'wait',
			detail: opening
				? `Back ${opening}. Waiting work starts automatically.`
				: compactText(host.schedule_detail) ||
					'Waiting work starts automatically in the next work window.',
			transitionAt: host.schedule_next_opens_at
		};
	}
	if (hostHasDrainCandidate(host, queue)) {
		const close = transitionCopy(host.schedule_closes_at, host, now);
		const countdown = formatScheduleCountdown(host.schedule_closes_at, now);
		return {
			state: 'host_draining',
			label: 'Draining',
			tone: 'wait',
			detail: [
				close ? `Open until ${close}` : 'Open now',
				countdown ? `${countdown} left` : null,
				host.active_encode_count > 0
					? 'current work continues, but no new item safely fits the time left'
					: 'no queued item safely fits the time left'
			]
				.filter(Boolean)
				.join(' · '),
			transitionAt: host.schedule_closes_at
		};
	}
	if (host.schedule_open === true && host.schedule_closes_at) {
		const close = transitionCopy(host.schedule_closes_at, host, now);
		const countdown = formatScheduleCountdown(host.schedule_closes_at, now);
		return {
			state: 'host_open',
			label: host.active_encode_count > 0 ? 'Working' : 'Open',
			tone: host.active_encode_count > 0 ? 'active' : 'ready',
			detail: [
				close ? `Until ${close}` : compactText(host.schedule_detail),
				countdown ? `${countdown} left` : null
			]
				.filter(Boolean)
				.join(' · '),
			transitionAt: host.schedule_closes_at
		};
	}
	return null;
}

export function allQueueWorkersScheduledOffWindow(hosts: HostRuntime[]): boolean {
	const queueHosts = hosts.filter((host) => host.capabilities.includes('encode_queue'));
	if (!queueHosts.length) return false;
	return queueHosts.every(
		(host) =>
			host.available && host.schedule_profile_label !== 'Never' && host.schedule_open === false
	);
}

export function nextQueueWindowCopy(hosts: HostRuntime[], now = new Date()): string | null {
	const nextOpening = hosts
		.filter(
			(host) =>
				host.capabilities.includes('encode_queue') &&
				host.available &&
				host.schedule_profile_label !== 'Never' &&
				Boolean(host.schedule_next_opens_at)
		)
		.map((host) => ({ host, opensAt: compactText(host.schedule_next_opens_at) }))
		.filter((candidate) => Number.isFinite(Date.parse(candidate.opensAt)))
		.sort((left, right) => Date.parse(left.opensAt) - Date.parse(right.opensAt))[0];
	if (!nextOpening) return null;
	const opening = transitionCopy(nextOpening.opensAt, nextOpening.host, now);
	return opening ? `${opening} on ${nextOpening.host.label}` : null;
}
