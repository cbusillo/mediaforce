<script lang="ts">
	import type { HostRuntime } from '$lib/api/types';
	import { folderAwareQualitySearchSummary, qualitySearchSummary } from '$lib/hosts/runtime';
	import Panel from '$lib/components/Panel.svelte';

	let {
		host,
		onSettingsClick = null,
		folderPrefix = null
	}: {
		host: HostRuntime;
		onSettingsClick?: (() => void) | null;
		folderPrefix?: string | null;
	} = $props();

	type StateTone = 'ok' | 'warn' | 'hold' | 'neutral';
	const capabilityLabels: Record<string, string> = {
		encode_queue: 'Queue',
		sample_calibration: 'Sample'
	};

	const hostTone = $derived.by(() => {
		if (!host.available) {
			return 'warn' as StateTone;
		}

		if (host.queue_active) {
			return 'ok' as StateTone;
		}

		if (host.schedule_open === false) {
			return 'hold' as StateTone;
		}

		if (host.active_reason === 'parallel encode slots are full') {
			return 'neutral' as StateTone;
		}

		if (host.active_reason === 'encode queue capability disabled') {
			return 'warn' as StateTone;
		}

		return 'neutral' as StateTone;
	});

	const hostDetail = $derived.by(() => {
		if (!host.schedule_detail || ['Always', 'Never'].includes(host.schedule_profile_label)) {
			return null;
		}

		return host.schedule_detail
			.replace(/^window\s+/i, '')
			.replace(/\s+in host local time$/i, ' local time');
	});

	const attentionCard = $derived.by(() => {
		if (!host.available) {
			return {
				label: 'Needs attention',
				message: host.message,
				detail: host.detail,
				linkLabel: onSettingsClick ? 'Open host settings' : null
			};
		}

		if (host.active_reason === 'encode queue capability disabled') {
			return {
				label: 'Needs setup',
				message: 'Encode queue capability disabled',
				detail: onSettingsClick ? 'Enable queue support for this worker in Settings.' : null,
				linkLabel: onSettingsClick ? 'Open host settings' : null
			};
		}

		return null;
	});

	const statusChip = $derived.by(() => {
		if (attentionCard) {
			return null;
		}

		if (host.queue_active) {
			return { label: 'Ready', tone: 'ok' as const };
		}

		if (host.schedule_profile_label === 'Never') {
			return { label: 'Disabled', tone: 'hold' as const };
		}

		if (host.schedule_open === false) {
			return { label: 'Scheduled', tone: 'hold' as const };
		}

		if (host.active_reason === 'parallel encode slots are full') {
			return { label: 'At Capacity', tone: 'neutral' as const };
		}

		return { label: 'Mounted', tone: 'neutral' as const };
	});

	const statusTrayLabel = $derived(host.schedule_profile_label);

	const statusTrayHeadline = $derived.by(() => {
		const laneLabel = `lane${host.max_parallel_encodes === 1 ? '' : 's'}`;

		if (host.active_encode_count > 0) {
			return `${host.active_encode_count} of ${host.max_parallel_encodes} ${laneLabel} running`;
		}

		if (host.queue_active) {
			return `${host.max_parallel_encodes} ${laneLabel} available`;
		}

		if (host.schedule_profile_label === 'Never') {
			return `${host.max_parallel_encodes} ${laneLabel} disabled`;
		}

		if (host.schedule_open === false) {
			return `${host.max_parallel_encodes} ${laneLabel} scheduled for the next window`;
		}

		if (host.active_reason === 'parallel encode slots are full') {
			return `${host.max_parallel_encodes} ${laneLabel} busy`;
		}

		return `${host.max_parallel_encodes} ${laneLabel} mounted`;
	});

	const metadataPills = $derived.by(() => [
		`P${host.priority}`,
		...host.capabilities.map(
			(capability) => capabilityLabels[capability] ?? capability.replace(/_/g, ' ')
		)
	]);
	const runningJobsSummary = $derived.by(() => {
		const count = host.running_jobs?.length ?? 0;
		if (count <= 0) return '';
		if (count === 1) return '1 live encode';
		return `${count} live encodes`;
	});
	const searchSummary = $derived(
		folderPrefix ? folderAwareQualitySearchSummary(host, folderPrefix) : qualitySearchSummary(host)
	);
</script>

<Panel class={`host-card ${hostTone}`.trim()} padding="0.8rem 0.9rem 0.92rem">
	<div class="head-row">
		<div class="title-block">
			<h3>{host.label}</h3>
			<p class="muted-copy host-key">{host.key}</p>
		</div>
		{#if statusChip}
			{#if statusChip.tone === 'hold' && onSettingsClick}
				<button
					type="button"
					class={`state-chip state-chip-button ${statusChip.tone}`.trim()}
					onclick={onSettingsClick}
				>
					{statusChip.label}
				</button>
			{:else}
				<span class={`state-chip ${statusChip.tone}`.trim()}>{statusChip.label}</span>
			{/if}
		{/if}
	</div>

	{#if !attentionCard}
		<div class={`status-tray ${hostTone}`.trim()}>
			<div class="tray-copy">
				<p class="tray-kicker">{statusTrayLabel}</p>
				<p class="tray-headline">{statusTrayHeadline}</p>
				{#if hostDetail}
					<p class="host-detail muted-copy">{hostDetail}</p>
				{/if}
			</div>
			<div class="meta-pill-row" aria-label="Host metadata">
				{#each metadataPills as pill (pill)}
					<span class={`meta-pill ${pill.startsWith('P') ? 'priority' : ''}`.trim()}>{pill}</span>
				{/each}
			</div>
		</div>
		{#if searchSummary}
			<p class="search-mode-copy muted-copy">
				<span>{searchSummary.label}</span>
				<span>{searchSummary.detail}</span>
			</p>
		{/if}
		{#if host.running_jobs && host.running_jobs.length > 0}
			<details class="running-job-shell" aria-label="Active encode jobs on this host">
				<summary>
					<span>{runningJobsSummary}</span>
					<span class="muted-copy">Open details</span>
				</summary>
				<div class="running-job-list">
					{#each host.running_jobs as job (job.job_id)}
						<div class="running-job-row">
							<div class="running-job-copy">
								<p class="running-job-title">{job.prefix}</p>
								<p class="muted-copy running-job-detail">
									{job.progress?.current_item_rel_path ?? 'Preparing encode job'}
								</p>
							</div>
							<p class="running-job-summary">
								{job.telemetry_summary || job.scheduler_status_copy || 'Running now'}
							</p>
						</div>
					{/each}
				</div>
			</details>
		{/if}
	{/if}

	{#if attentionCard}
		<div class="issues-box">
			<p class="attention-kicker">{attentionCard.label}</p>
			<p>{attentionCard.message}</p>
			{#if host.issues.length > 0 || host.missing_paths.length > 0}
				<ul class="issue-list">
					{#each host.issues as issue (issue)}
						<li>{issue}</li>
					{/each}
					{#each host.missing_paths as path (path)}
						<li>Missing path: {path}</li>
					{/each}
				</ul>
			{/if}
			{#if attentionCard.detail}
				<p class="muted-copy">{attentionCard.detail}</p>
			{/if}
			{#if onSettingsClick && attentionCard.linkLabel}
				<button type="button" class="attention-link" onclick={onSettingsClick}>
					{attentionCard.linkLabel}
				</button>
			{/if}
		</div>
	{/if}
</Panel>

<style>
	:global(.host-card) {
		--host-card-bg: rgba(12, 17, 24, 0.96);
		--host-card-bg-soft: rgba(16, 23, 32, 0.96);
		--host-card-border: rgba(148, 163, 184, 0.18);
		--host-card-ink: #f8fafc;
		--host-card-muted: rgba(226, 232, 240, 0.82);
		--host-card-soft: rgba(203, 213, 225, 0.72);
		display: grid;
		gap: 0.7rem;
		align-content: start;
		background: var(--host-card-bg);
		border-color: var(--host-card-border);
		color: var(--host-card-ink);
	}

	:global(.host-card)::before {
		content: '';
		position: absolute;
		inset: 0 auto 0 0;
		width: 4px;
		background: rgba(23, 35, 31, 0.1);
		pointer-events: none;
	}

	:global(.host-card.ok)::before {
		background: linear-gradient(180deg, rgba(47, 107, 62, 0.9), rgba(47, 107, 62, 0.28));
	}

	:global(.host-card.ok) {
		background:
			linear-gradient(180deg, rgba(18, 46, 32, 0.68), rgba(12, 17, 24, 0.96)), var(--host-card-bg);
	}

	:global(.host-card.warn)::before {
		background: linear-gradient(180deg, rgba(194, 65, 12, 0.9), rgba(194, 65, 12, 0.28));
	}

	:global(.host-card.warn) {
		background:
			linear-gradient(180deg, rgba(68, 32, 18, 0.7), rgba(12, 17, 24, 0.96)), var(--host-card-bg);
	}

	:global(.host-card.hold)::before {
		background: linear-gradient(180deg, rgba(180, 83, 9, 0.85), rgba(180, 83, 9, 0.24));
	}

	:global(.host-card.hold) {
		background:
			linear-gradient(180deg, rgba(60, 38, 14, 0.72), rgba(12, 17, 24, 0.96)), var(--host-card-bg);
	}

	:global(.host-card.neutral)::before {
		background: linear-gradient(180deg, rgba(82, 101, 94, 0.62), rgba(82, 101, 94, 0.18));
	}

	:global(.host-card.neutral) {
		background:
			linear-gradient(180deg, rgba(22, 30, 40, 0.98), rgba(12, 17, 24, 0.96)), var(--host-card-bg);
	}

	.head-row {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		column-gap: var(--space-2);
		align-items: start;
	}

	.title-block {
		min-width: 0;
	}

	h3 {
		font-size: 1.08rem;
		font-weight: 700;
		line-height: 1.1;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		color: var(--host-card-ink);
	}

	.host-key {
		font-size: 0.84rem;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.state-chip {
		display: inline-flex;
		align-items: center;
		padding: 0.32rem 0.62rem;
		border-radius: 999px;
		font-size: 0.74rem;
		font-weight: 700;
		line-height: 1;
		white-space: nowrap;
		border: 1px solid rgba(148, 163, 184, 0.22);
		background: rgba(30, 41, 59, 0.88);
		color: var(--host-card-muted);
	}

	.state-chip.ok {
		background: rgba(34, 197, 94, 0.16);
		border-color: rgba(74, 222, 128, 0.36);
		color: #bbf7d0;
	}

	.state-chip.ok::before {
		content: '';
		width: 0.38rem;
		height: 0.38rem;
		margin-right: 0.34rem;
		border-radius: 999px;
		background: currentColor;
	}

	.state-chip.hold {
		background: rgba(217, 119, 6, 0.16);
		border-color: rgba(251, 191, 36, 0.34);
		color: #fde68a;
	}

	.state-chip.neutral {
		background: rgba(30, 41, 59, 0.92);
		border-color: rgba(148, 163, 184, 0.28);
		color: var(--host-card-muted);
	}

	.state-chip-button {
		cursor: pointer;
		transition:
			transform 150ms ease,
			background-color 150ms ease,
			border-color 150ms ease;
	}

	.state-chip-button:hover {
		transform: translateY(-1px);
	}

	.status-tray {
		display: flex;
		flex-direction: column;
		gap: 0.7rem;
		padding: 0.82rem 0.88rem;
		border-radius: var(--radius-md);
		border: 1px solid var(--host-card-border);
		box-sizing: border-box;
	}

	.status-tray.ok {
		background: rgba(20, 83, 45, 0.2);
		border-color: rgba(74, 222, 128, 0.24);
	}

	.status-tray.hold {
		background: rgba(146, 64, 14, 0.2);
		border-color: rgba(251, 191, 36, 0.24);
	}

	.status-tray.neutral {
		background: rgba(15, 23, 42, 0.72);
		border-color: rgba(148, 163, 184, 0.2);
	}

	.tray-copy,
	.host-detail {
		margin: 0;
	}

	.tray-copy {
		display: grid;
		gap: 0.18rem;
	}

	.tray-kicker {
		margin: 0;
		font-size: 0.69rem;
		font-weight: 700;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--accent-deep);
	}

	:global(.host-card.ok) .tray-kicker {
		color: #86efac;
	}

	:global(.host-card.hold) .tray-kicker {
		color: #fcd34d;
	}

	.tray-headline {
		margin: 0;
		font-size: 0.95rem;
		font-weight: 700;
		line-height: 1.2;
		color: var(--host-card-ink);
	}

	.host-detail {
		font-size: 0.76rem;
		line-height: 1.35;
		color: var(--host-card-muted);
	}

	.search-mode-copy {
		margin: -0.12rem 0 0;
		display: grid;
		gap: 0.08rem;
		font-size: 0.76rem;
		line-height: 1.35;
	}

	.search-mode-copy span:first-child {
		font-weight: 700;
		color: var(--host-card-muted);
	}

	.meta-pill-row {
		display: flex;
		flex-wrap: wrap;
		gap: 0.36rem;
		padding-top: 0.2rem;
	}

	.meta-pill {
		display: inline-flex;
		align-items: center;
		padding: 0.24rem 0.5rem;
		border-radius: 999px;
		background: rgba(30, 41, 59, 0.88);
		border: 1px solid rgba(148, 163, 184, 0.22);
		font-size: 0.72rem;
		font-weight: 700;
		line-height: 1;
		color: var(--host-card-muted);
	}

	.meta-pill.priority {
		background: rgba(14, 165, 233, 0.16);
		border-color: rgba(125, 211, 252, 0.3);
		color: #e0f2fe;
	}

	.issues-box {
		display: flex;
		flex-direction: column;
		gap: 0.36rem;
		padding: 0.82rem 0.88rem;
		border-radius: var(--radius-md);
		background: rgba(69, 26, 3, 0.46);
		border: 1px solid rgba(251, 146, 60, 0.28);
		height: 100%;
		box-sizing: border-box;
	}

	.attention-kicker {
		margin: 0;
		font-size: 0.72rem;
		font-weight: 700;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: #fdba74;
	}

	.issues-box p:nth-child(2) {
		margin: 0;
		font-weight: 700;
		color: #fed7aa;
	}

	.issues-box p {
		margin: 0;
	}

	.issue-list {
		margin: 0;
		padding-left: 1rem;
		display: grid;
		gap: 0.22rem;
		color: var(--host-card-muted);
		font-size: 0.88rem;
		line-height: 1.45;
	}

	.running-job-list {
		display: grid;
		gap: 0.42rem;
		padding-top: 0.1rem;
	}

	.running-job-shell {
		display: grid;
		gap: 0.55rem;
		padding: 0.72rem 0.8rem;
		border-radius: var(--radius-md);
		background: rgba(15, 23, 42, 0.72);
		border: 1px solid rgba(148, 163, 184, 0.2);
	}

	.running-job-shell summary {
		cursor: pointer;
		display: flex;
		justify-content: space-between;
		gap: 0.75rem;
		align-items: center;
		font-size: 0.84rem;
		font-weight: 700;
		color: var(--host-card-ink);
	}

	.running-job-shell summary::-webkit-details-marker {
		display: none;
	}

	.running-job-row {
		display: grid;
		grid-template-columns: minmax(0, 1fr);
		gap: 0.38rem;
		align-items: start;
		padding: 0.68rem 0.78rem;
		border-radius: var(--radius-md);
		background: rgba(30, 41, 59, 0.76);
		border: 1px solid rgba(148, 163, 184, 0.18);
	}

	.running-job-copy {
		min-width: 0;
	}

	.running-job-title,
	.running-job-summary,
	.running-job-detail {
		margin: 0;
	}

	.running-job-title {
		font-size: 0.9rem;
		font-weight: 700;
		line-height: 1.18;
		color: var(--host-card-ink);
		overflow-wrap: anywhere;
	}

	.running-job-detail {
		margin-top: 0.18rem;
		font-size: 0.84rem;
		line-height: 1.4;
		overflow-wrap: anywhere;
	}

	.running-job-summary {
		font-size: 0.84rem;
		font-weight: 700;
		line-height: 1.3;
		color: var(--host-card-muted);
		overflow-wrap: anywhere;
	}

	.attention-link {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: fit-content;
		margin-top: auto;
		padding: 0.42rem 0.68rem;
		border-radius: 999px;
		border: 1px solid rgba(194, 65, 12, 0.16);
		background: rgba(69, 26, 3, 0.7);
		color: #fed7aa;
		font-size: 0.76rem;
		font-weight: 700;
		text-decoration: none;
		cursor: pointer;
		transition:
			transform 150ms ease,
			background-color 150ms ease,
			border-color 150ms ease;
	}

	.attention-link:hover {
		transform: translateY(-1px);
		background: rgba(124, 45, 18, 0.72);
	}
</style>
