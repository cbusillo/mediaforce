<script lang="ts">
	import { resolve } from '$app/paths';
	import type { HostRuntime } from '$lib/api/types';

	let {
		rankedHosts,
		readyHosts,
		onOpenHostSettings
	}: {
		rankedHosts: HostRuntime[];
		readyHosts: number;
		onOpenHostSettings: (hostKey: string) => Promise<void>;
	} = $props();

	type HostTone = 'ok' | 'warn' | 'hold' | 'neutral';

	const capabilityLabels: Record<string, string> = {
		encode_queue: 'Queue',
		sample_calibration: 'Sample'
	};

	function hostTone(host: HostRuntime): HostTone {
		if (!host.available) return 'warn';
		if (host.queue_active) return 'ok';
		if (host.active_reason === 'encode queue capability disabled') return 'warn';
		if (host.schedule_open === false) return 'hold';
		return 'neutral';
	}

	function hostStatusLabel(host: HostRuntime): string {
		if (!host.available) return 'Needs attention';
		if (host.queue_active) return 'Ready';
		if (host.schedule_profile_label === 'Never') return 'Disabled';
		if (host.active_reason === 'parallel encode slots are full') return 'At capacity';
		if (host.active_reason === 'encode queue capability disabled') return 'Needs setup';
		if (host.schedule_open === false) return 'Scheduled';
		return 'Mounted';
	}

	function laneCopy(host: HostRuntime): string {
		const laneLabel = `lane${host.max_parallel_encodes === 1 ? '' : 's'}`;
		if (host.active_encode_count > 0) {
			return `${host.active_encode_count}/${host.max_parallel_encodes} ${laneLabel} running`;
		}
		if (host.queue_active) return `${host.max_parallel_encodes} ${laneLabel} available`;
		if (host.schedule_profile_label === 'Never')
			return `${host.max_parallel_encodes} ${laneLabel} disabled`;
		if (host.active_reason === 'parallel encode slots are full') {
			return `${host.max_parallel_encodes} ${laneLabel} busy`;
		}
		if (host.active_reason === 'encode queue capability disabled') {
			return `${host.max_parallel_encodes} ${laneLabel} need queue setup`;
		}
		if (host.schedule_open === false) {
			return `${host.max_parallel_encodes} ${laneLabel} scheduled for the next window`;
		}
		return `${host.max_parallel_encodes} ${laneLabel} mounted`;
	}

	function scheduleCopy(host: HostRuntime): string {
		if (!host.schedule_detail || ['Always', 'Never'].includes(host.schedule_profile_label)) {
			return host.schedule_profile_label;
		}
		return `${host.schedule_profile_label}: ${host.schedule_detail
			.replace(/^window\s+/i, '')
			.replace(/\s+in host local time$/i, ' local time')}`;
	}

	function capabilityCopy(host: HostRuntime): string {
		const labels = host.capabilities.map(
			(capability) => capabilityLabels[capability] ?? capability.replace(/_/g, ' ')
		);
		return [`P${host.priority}`, ...labels].join(' · ');
	}

	function attentionLabel(host: HostRuntime): string | null {
		if (!host.available) {
			return host.message || 'Host is unavailable.';
		}
		if (host.active_reason === 'encode queue capability disabled') {
			return 'Needs setup';
		}
		if (hostBlockers(host).length > 0) return 'Needs attention';
		return null;
	}

	function attentionDetail(host: HostRuntime): string | null {
		if (!host.available) return hostIssueCopy(host);
		if (host.active_reason === 'encode queue capability disabled') {
			return 'Enable queue support for this worker in Settings.';
		}
		return null;
	}

	function hostIssueCopy(host: HostRuntime): string | null {
		const blockers = hostBlockers(host);
		if (blockers.length > 0) return blockers[0];
		return host.detail;
	}

	function hostBlockers(host: HostRuntime): string[] {
		return [...host.issues, ...host.missing_paths.map((path) => `Missing path: ${path}`)].filter(
			(item) => item.trim().length > 0
		);
	}
</script>

<section class="host-console" aria-label="Remote hosts">
	<div class="section-header-row">
		<div>
			<p class="section-kicker">Remote Hosts</p>
			<h2>Worker readiness</h2>
			<p class="section-lede">
				Workers ranked by priority, queue availability, and current setup state.
			</p>
		</div>
		<div class="section-header-tools">
			<span class={`section-summary-chip ${readyHosts > 0 ? 'active' : ''}`.trim()}
				>{readyHosts} ready</span
			>
			<a class="section-action-link" href={resolve('/settings')}>Edit in Settings</a>
		</div>
	</div>

	<div id="remote-hosts" class="host-list" role="table" aria-label="Remote host readiness table">
		<div class="host-table-head" role="row">
			<span role="columnheader">Host</span>
			<span role="columnheader">State</span>
			<span role="columnheader">Lanes / schedule</span>
			<span role="columnheader">Capability</span>
			<span role="columnheader">Attention</span>
		</div>
		{#each rankedHosts as host (host.key)}
			{@const tone = hostTone(host)}
			{@const blockers = hostBlockers(host)}
			{@const attention = attentionLabel(host)}
			{@const detail = attentionDetail(host)}
			<div class={`host-row ${tone}`.trim()} role="row">
				<div class="host-name-cell" role="cell">
					<p class="host-label">{host.label}</p>
					<p class="host-key muted-copy">{host.key}</p>
				</div>
				<div class="host-status-cell" role="cell">
					<button
						type="button"
						class={`state-chip ${tone}`.trim()}
						onclick={() => onOpenHostSettings(host.key)}
					>
						{hostStatusLabel(host)}
					</button>
				</div>
				<div class="host-lanes-cell" role="cell">
					<p class="cell-strong">{laneCopy(host)}</p>
					<p class="muted-copy">{scheduleCopy(host)}</p>
				</div>
				<div class="host-capability-cell" role="cell">
					<p class="cell-strong">{capabilityCopy(host)}</p>
					{#if host.running_jobs && host.running_jobs.length > 0}
						<details class="running-job-shell" aria-label={`Active encode jobs on ${host.label}`}>
							<summary>
								{host.running_jobs.length} live encode{host.running_jobs.length === 1 ? '' : 's'}
							</summary>
							<div class="running-job-list">
								{#each host.running_jobs as job (job.job_id)}
									<div class="running-job-row">
										<p class="running-job-title">{job.prefix}</p>
										<p class="muted-copy running-job-detail">
											{job.progress?.current_item_rel_path ?? 'Preparing encode job'}
										</p>
										<p class="running-job-summary">
											{job.telemetry_summary || job.scheduler_status_copy || 'Running now'}
										</p>
									</div>
								{/each}
							</div>
						</details>
					{:else}
						<p class="muted-copy">
							{host.media_access === 'stream' ? 'Streams media' : 'Mounted media'}
						</p>
					{/if}
				</div>
				<div class="host-attention-cell" role="cell">
					{#if attention}
						<p class="attention-copy">{attention}</p>
					{/if}
					{#if blockers.length > 0}
						<ul class="blocker-list" aria-label={`Host blockers for ${host.label}`}>
							{#each blockers as blocker (blocker)}
								<li>{blocker}</li>
							{/each}
						</ul>
					{:else if detail}
						<p class="muted-copy issue-copy">{detail}</p>
					{/if}
				</div>
			</div>
		{/each}
	</div>
</section>

<style>
	.host-console {
		display: grid;
		gap: 0;
		border: 1px solid rgba(148, 163, 184, 0.18);
		background: rgba(15, 20, 27, 0.94);
		box-shadow: 0 18px 38px rgba(2, 6, 23, 0.2);
	}

	.section-header-row {
		display: flex;
		justify-content: space-between;
		gap: 1rem;
		align-items: start;
		flex-wrap: wrap;
		padding: 0.95rem 1.1rem;
		border-bottom: 1px solid rgba(148, 163, 184, 0.16);
	}

	.section-kicker,
	.section-lede,
	h2,
	.host-label,
	.host-key,
	.cell-strong,
	.attention-copy,
	.issue-copy {
		margin: 0;
	}

	.section-kicker {
		font-size: 0.72rem;
		font-weight: 800;
		letter-spacing: 0.16em;
		text-transform: uppercase;
		color: rgba(125, 211, 252, 0.86);
	}

	h2 {
		margin-top: 0.2rem;
		font-size: 1.08rem;
		line-height: 1.2;
		color: #f8fafc;
	}

	.section-lede {
		margin-top: 0.35rem;
		color: rgba(226, 232, 240, 0.72);
	}

	.section-header-tools {
		display: flex;
		gap: 0.8rem;
		align-items: center;
		flex-wrap: wrap;
	}

	.section-summary-chip {
		display: inline-flex;
		align-items: center;
		gap: 0.35rem;
		padding: 0.42rem 0.72rem;
		border-radius: var(--radius-pill);
		font-size: 0.78rem;
		font-weight: 700;
		letter-spacing: 0.03em;
		text-transform: uppercase;
		background: rgba(30, 41, 59, 0.78);
		color: rgba(226, 232, 240, 0.78);
	}

	.section-summary-chip.active {
		background: rgba(20, 83, 45, 0.82);
		color: #dcfce7;
	}

	.section-action-link {
		font-weight: 700;
		color: #7dd3fc;
		text-decoration: none;
	}

	.host-list {
		display: grid;
	}

	.host-table-head,
	.host-row {
		display: grid;
		grid-template-columns:
			minmax(11rem, 0.9fr) minmax(7.5rem, 0.55fr) minmax(12rem, 1fr) minmax(11rem, 0.8fr)
			minmax(16rem, 1.3fr);
		gap: 0.85rem;
		align-items: start;
	}

	.host-table-head {
		padding: 0.58rem 1.1rem;
		border-bottom: 1px solid rgba(148, 163, 184, 0.12);
		color: rgba(148, 163, 184, 0.88);
		font-size: 0.68rem;
		font-weight: 800;
		letter-spacing: 0.13em;
		text-transform: uppercase;
	}

	.host-row {
		position: relative;
		padding: 0.88rem 1.1rem;
		border-bottom: 1px solid rgba(148, 163, 184, 0.12);
	}

	.host-row:last-child {
		border-bottom: 0;
	}

	.host-row::before {
		content: '';
		position: absolute;
		inset: 0 auto 0 0;
		width: 3px;
		background: rgba(71, 85, 105, 0.7);
	}

	.host-row.ok::before {
		background: rgba(34, 197, 94, 0.9);
	}

	.host-row.warn::before {
		background: rgba(249, 115, 22, 0.94);
	}

	.host-row.hold::before {
		background: rgba(245, 158, 11, 0.9);
	}

	.host-label,
	.cell-strong,
	.attention-copy {
		color: #f8fafc;
		font-weight: 700;
	}

	.host-key,
	.host-row .muted-copy {
		color: rgba(226, 232, 240, 0.7);
	}

	.host-key,
	.cell-strong,
	.attention-copy,
	.issue-copy,
	.host-row .muted-copy {
		overflow-wrap: anywhere;
	}

	.state-chip {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: fit-content;
		padding: 0.36rem 0.66rem;
		border: 1px solid rgba(148, 163, 184, 0.18);
		border-radius: var(--radius-pill);
		background: rgba(30, 41, 59, 0.82);
		color: rgba(226, 232, 240, 0.82);
		font-size: 0.74rem;
		font-weight: 800;
		line-height: 1;
		cursor: pointer;
	}

	.state-chip.ok {
		background: rgba(20, 83, 45, 0.82);
		color: #dcfce7;
	}

	.state-chip.warn,
	.state-chip.hold {
		background: rgba(120, 53, 15, 0.82);
		color: #ffedd5;
	}

	.issue-copy {
		margin-top: 0.25rem;
	}

	.blocker-list,
	.running-job-list {
		display: grid;
		gap: 0.45rem;
		margin: 0.4rem 0 0;
		padding: 0;
		list-style: none;
	}

	.blocker-list li {
		position: relative;
		padding-left: 0.72rem;
		color: rgba(226, 232, 240, 0.72);
		line-height: 1.35;
		overflow-wrap: anywhere;
	}

	.blocker-list li::before {
		content: '';
		position: absolute;
		left: 0;
		top: 0.62em;
		width: 0.28rem;
		height: 0.28rem;
		border-radius: 999px;
		background: rgba(249, 115, 22, 0.88);
	}

	.running-job-shell {
		margin-top: 0.32rem;
	}

	.running-job-shell summary {
		cursor: pointer;
		width: fit-content;
		color: rgba(125, 211, 252, 0.88);
		font-size: 0.84rem;
		font-weight: 700;
	}

	.running-job-shell summary::-webkit-details-marker {
		display: none;
	}

	.running-job-row {
		display: grid;
		gap: 0.24rem;
		padding-top: 0.48rem;
		border-top: 1px solid rgba(148, 163, 184, 0.14);
	}

	.running-job-row:first-child {
		padding-top: 0;
		border-top: 0;
	}

	.running-job-title,
	.running-job-summary,
	.running-job-detail {
		margin: 0;
		overflow-wrap: anywhere;
	}

	.running-job-title {
		font-weight: 700;
		color: #f8fafc;
	}

	.running-job-summary {
		width: fit-content;
		padding: 0.18rem 0.4rem;
		border-radius: var(--radius-pill);
		background: rgba(14, 165, 233, 0.14);
		color: rgba(186, 230, 253, 0.94);
		font-size: 0.76rem;
		font-weight: 800;
	}

	@media (max-width: 1120px) {
		.host-table-head {
			display: none;
		}

		.host-row {
			grid-template-columns: minmax(12rem, 1fr) minmax(7rem, auto);
			gap: 0.65rem 1rem;
		}

		.host-lanes-cell,
		.host-capability-cell,
		.host-attention-cell {
			grid-column: 1 / -1;
		}
	}

	@media (max-width: 640px) {
		.host-row {
			grid-template-columns: 1fr;
		}
	}
</style>
