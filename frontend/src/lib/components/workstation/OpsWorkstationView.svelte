<script lang="ts">
	import { invalidateAll } from '$app/navigation';
	import { onDestroy, onMount } from 'svelte';
	import { resolve } from '$app/paths';
	import { postJson } from '$lib/api/client';
	import type { DashboardSummaryPayload, HostRuntime, HostsPayload } from '$lib/api/types';
	import { folderRoutePath } from '$lib/folder-display';
	import OperatorShell from './OperatorShell.svelte';
	import StateBadge from './StateBadge.svelte';
	import WorkstationPanel from './WorkstationPanel.svelte';
	import {
		buildOpsBlockers,
		buildOpsFooterSignals,
		buildOpsQueueRows,
		buildOpsStatusTiles,
		hostPrepareDisabled,
		hostPrepareTitle,
		hostStateCopy,
		hostTone,
		rowRecoveryLabel,
		rowRecoveryTitle,
		type OpsActionId,
		type OpsQueueRow
	} from './ops-workstation';

	const OPS_REFRESH_INTERVAL_MS = 15_000;

	let {
		dashboard,
		hosts,
		loadError
	}: {
		dashboard: DashboardSummaryPayload | null;
		hosts: HostsPayload | null;
		loadError: string | null;
	} = $props();

	const queueRows = $derived(buildOpsQueueRows(dashboard));
	const blockers = $derived(buildOpsBlockers(dashboard, hosts, loadError));
	const statusTiles = $derived(buildOpsStatusTiles(dashboard, hosts, loadError));
	const footerSignals = $derived(buildOpsFooterSignals(dashboard, hosts));
	const encodeQueue = $derived(dashboard?.encode_queue ?? null);
	const calibrationQueue = $derived(dashboard?.calibration_queue ?? null);
	const activeJobCount = $derived(
		(encodeQueue?.running_count ?? 0) +
			(encodeQueue?.queued_count ?? 0) +
			(calibrationQueue?.active_count ?? 0)
	);
	const queuedWaitingCount = $derived(encodeQueue?.queued_waiting_count ?? 0);
	const needsAttentionCount = $derived(encodeQueue?.needs_attention_count ?? 0);
	const readyHosts = $derived(hosts?.hosts.filter((host) => host.available).length ?? 0);
	const closedHosts = $derived(hosts?.hosts.filter((host) => host.schedule_open === false) ?? []);

	let actionPending = $state<OpsActionId | null>(null);
	let confirmationAction = $state<OpsActionId | null>(null);
	let actionMessage = $state('');
	let actionError = $state('');
	let refreshPending = $state(false);
	let refreshError = $state('');
	let lastRefreshAt = $state<Date | null>(null);
	let hostPasswords = $state<Record<string, string>>({});
	let refreshTimer: ReturnType<typeof setInterval> | null = null;

	const actionEndpoints: Record<OpsActionId, string> = {
		'pause-encode': '/api/encode-queue/pause',
		'resume-encode': '/api/encode-queue/resume',
		'retry-failed-encode': '/api/encode-queue/retry-failed',
		'retry-encode-prefix': '/api/encode-queue/retry-prefix',
		'stop-encode': '/api/encode-queue/stop',
		'stop-calibration': '/api/calibration-queue/stop',
		'start-host': '/api/hosts/start',
		'prepare-host': '/api/hosts/prepare',
		'reset-host-trust': '/api/hosts/reset-trust'
	};

	function actionRequiresConfirmation(action: OpsActionId): boolean {
		return action === 'stop-encode' || action === 'stop-calibration';
	}

	function actionTitle(action: OpsActionId): string {
		if (action === 'stop-encode') return 'Stop running encode workers and pause the queue';
		if (action === 'stop-calibration') return 'Stop running and queued sample/proof jobs';
		if (action === 'retry-failed-encode')
			return 'Retry failed folder encodes that are still approved';
		if (action === 'retry-encode-prefix') return 'Retry this failed folder encode';
		if (action === 'pause-encode') return 'Pause the encode scheduler';
		if (action === 'resume-encode') return 'Resume the encode scheduler and clear stop request';
		if (action === 'start-host') return 'Start or wake this host';
		if (action === 'prepare-host') return 'Prepare this host for Mediaforce work';
		return 'Reset stored trust for this host';
	}

	function actionBody(
		action: OpsActionId,
		host?: HostRuntime,
		row?: OpsQueueRow
	): Record<string, unknown> {
		if (action === 'retry-encode-prefix' && row) return { prefix: row.prefix };
		if (!host) return {};
		const body: Record<string, unknown> = { host_key: host.key };
		if (action === 'prepare-host' && host.setup_requires_password) {
			body.remote_password = hostPasswords[host.key] ?? '';
		}
		return body;
	}

	async function runAction(action: OpsActionId, host?: HostRuntime, row?: OpsQueueRow) {
		actionMessage = '';
		actionError = '';
		if (actionRequiresConfirmation(action) && confirmationAction !== action) {
			confirmationAction = action;
			actionMessage = 'Confirm the interrupting action with a second click.';
			return;
		}
		confirmationAction = null;
		actionPending = action;
		try {
			const endpoint = `${resolve('/')}${actionEndpoints[action].slice(1)}`;
			const response = await postJson<Record<string, unknown>>(
				endpoint,
				actionBody(action, host, row)
			);
			actionMessage =
				typeof response.message === 'string' && response.message.trim()
					? response.message
					: 'Action completed.';
			if (action === 'prepare-host' && host) {
				hostPasswords = { ...hostPasswords, [host.key]: '' };
			}
			await invalidateAll();
		} catch (error) {
			actionError = error instanceof Error ? error.message : 'Action failed.';
		} finally {
			actionPending = null;
		}
	}

	async function refreshOps({ quiet = false }: { quiet?: boolean } = {}) {
		if (refreshPending) return;
		refreshPending = true;
		if (!quiet) {
			actionMessage = '';
			actionError = '';
		}
		refreshError = '';
		try {
			await invalidateAll();
			lastRefreshAt = new Date();
			if (!quiet) actionMessage = 'Ops runtime refreshed.';
		} catch (error) {
			refreshError = error instanceof Error ? error.message : 'Refresh failed.';
			if (!quiet) actionError = refreshError;
		} finally {
			refreshPending = false;
		}
	}

	function refreshCopy(): string {
		if (refreshPending) return 'refreshing';
		if (!lastRefreshAt) return 'auto-refresh every 15s';
		return `refreshed ${lastRefreshAt.toLocaleTimeString([], {
			hour: '2-digit',
			minute: '2-digit',
			second: '2-digit'
		})}`;
	}

	function rowActionDisabled(row: OpsQueueRow): boolean {
		return actionPending !== null || !row.action;
	}

	function hostActionDisabled(action: OpsActionId, host: HostRuntime): boolean {
		if (actionPending !== null) return true;
		if (action === 'start-host') return host.available || !host.setup_supported;
		if (action === 'prepare-host') {
			return hostPrepareDisabled(host, hostPasswords[host.key] ?? '');
		}
		if (action === 'reset-host-trust') return !host.trust_reset_supported;
		return false;
	}

	function handleHostPasswordInput(host: HostRuntime, event: Event) {
		hostPasswords = {
			...hostPasswords,
			[host.key]: (event.currentTarget as HTMLInputElement).value
		};
	}

	function queueActionLabel(action: OpsActionId): string {
		if (confirmationAction === action) return 'Confirm';
		if (actionPending === action) return 'Working';
		if (action === 'pause-encode') return 'Pause';
		if (action === 'resume-encode') return 'Resume';
		if (action === 'retry-failed-encode') return 'Retry failed';
		if (action === 'retry-encode-prefix') return 'Retry folder';
		if (action === 'stop-encode') return 'Stop encode';
		if (action === 'stop-calibration') return 'Stop samples';
		if (action === 'start-host') return 'Start';
		if (action === 'prepare-host') return 'Prepare';
		if (action === 'reset-host-trust') return 'Trust';
		return 'Run';
	}

	function canOpenFolder(row: OpsQueueRow): boolean {
		return row.prefix !== 'runtime scope';
	}

	onMount(() => {
		lastRefreshAt = new Date();
		refreshTimer = setInterval(() => void refreshOps({ quiet: true }), OPS_REFRESH_INTERVAL_MS);
	});

	onDestroy(() => {
		if (refreshTimer) clearInterval(refreshTimer);
	});
</script>

<OperatorShell route="ops" subject="Ops" crumb="/ops" {statusTiles} {footerSignals}>
	<main class="ops">
		<section class="ops__main" aria-label="Ops workstation">
			<header class="ops-header">
				<div>
					<span class="mf-eyebrow">Ops</span>
					<h1>Encode and sample control</h1>
					<p>
						Monitor active work, scheduler pressure, blocked retries, and host readiness from the
						workstation shell.
					</p>
				</div>
				<div class="ops-header__facts">
					<div>
						<span>Active work</span>
						<strong>{activeJobCount.toLocaleString('en-US')}</strong>
					</div>
					<div>
						<span>Blocked</span>
						<strong>{needsAttentionCount.toLocaleString('en-US')}</strong>
					</div>
					<div>
						<span>Hosts ready</span>
						<strong>{readyHosts.toLocaleString('en-US')}</strong>
					</div>
					<button
						type="button"
						class="control control--compact"
						disabled={refreshPending}
						title="Refresh Ops runtime state now"
						onclick={() => refreshOps()}>{refreshPending ? 'Refreshing' : 'Refresh'}</button
					>
				</div>
			</header>

			<WorkstationPanel eyebrow="Control" title="Scheduler and recovery">
				<div class="scheduler-console">
					<div class="scheduler-console__state">
						<StateBadge
							tone={encodeQueue?.state.stop_requested
								? 'fail'
								: encodeQueue?.state.is_paused
									? 'wait'
									: 'ready'}
							label={encodeQueue?.state.stop_requested
								? 'Stopping'
								: encodeQueue?.state.is_paused
									? 'Paused'
									: 'Open'}
						/>
						<div>
							<strong>{encodeQueue?.state.scheduler_summary ?? 'Scheduler data unavailable'}</strong
							>
							<span
								>{queuedWaitingCount.toLocaleString('en-US')} waiting · {needsAttentionCount.toLocaleString(
									'en-US'
								)}
								need attention</span
							>
						</div>
					</div>
					<div class="scheduler-console__actions" aria-label="Scheduler controls">
						<button
							type="button"
							class="control"
							disabled={!encodeQueue ||
								encodeQueue.state.is_paused ||
								encodeQueue.state.stop_requested ||
								actionPending !== null}
							title={actionTitle('pause-encode')}
							onclick={() => runAction('pause-encode')}>{queueActionLabel('pause-encode')}</button
						>
						<button
							type="button"
							class="control control--ready"
							disabled={!encodeQueue ||
								(!encodeQueue.state.is_paused && !encodeQueue.state.stop_requested) ||
								actionPending !== null}
							title={actionTitle('resume-encode')}
							onclick={() => runAction('resume-encode')}>{queueActionLabel('resume-encode')}</button
						>
						<button
							type="button"
							class="control control--warn"
							disabled={!encodeQueue || needsAttentionCount === 0 || actionPending !== null}
							title={actionTitle('retry-failed-encode')}
							onclick={() => runAction('retry-failed-encode')}
							>{queueActionLabel('retry-failed-encode')}</button
						>
						<button
							type="button"
							class="control control--danger"
							class:control--armed={confirmationAction === 'stop-encode'}
							disabled={!encodeQueue ||
								(encodeQueue.running_count === 0 &&
									encodeQueue.queued_count === 0 &&
									!encodeQueue.state.is_paused) ||
								actionPending !== null}
							title={actionTitle('stop-encode')}
							onclick={() => runAction('stop-encode')}>{queueActionLabel('stop-encode')}</button
						>
						<button
							type="button"
							class="control control--danger"
							class:control--armed={confirmationAction === 'stop-calibration'}
							disabled={!calibrationQueue ||
								calibrationQueue.active_count === 0 ||
								actionPending !== null}
							title={actionTitle('stop-calibration')}
							onclick={() => runAction('stop-calibration')}
							>{queueActionLabel('stop-calibration')}</button
						>
					</div>
					{#if actionMessage}
						<p class="action-message">{actionMessage}</p>
					{/if}
					{#if actionError}
						<p class="action-error">{actionError}</p>
					{/if}
					<p class="refresh-note" class:refresh-note--error={Boolean(refreshError)}>
						{refreshError || refreshCopy()}
					</p>
				</div>
			</WorkstationPanel>

			<WorkstationPanel eyebrow="Blockers" title="Blocked and retryable work">
				<div class="blocker-list">
					{#each blockers.slice(0, 6) as blocker (blocker.key)}
						<div class="blocker-row blocker-row--{blocker.tone}">
							<StateBadge
								tone={blocker.tone}
								label={blocker.tone === 'fail' ? 'Blocked' : 'Waiting'}
							/>
							<div>
								<strong>{blocker.title}</strong>
								<span>{blocker.detail}</span>
							</div>
							{#if blocker.action}
								{@const action = blocker.action}
								<button
									type="button"
									class="control"
									disabled={actionPending !== null}
									title={actionTitle(action)}
									onclick={() => runAction(action)}>{queueActionLabel(action)}</button
								>
							{/if}
						</div>
					{:else}
						<div class="empty-note">
							No blocked or retryable work is visible in the runtime payload.
						</div>
					{/each}
				</div>
			</WorkstationPanel>

			<WorkstationPanel
				eyebrow="Jobs"
				title="Running, queued, and recovery queue"
				meta={`${queueRows.length.toLocaleString('en-US')} visible`}
			>
				<div class="table-wrap">
					<table>
						<thead>
							<tr>
								<th>State</th>
								<th>Work</th>
								<th>Host</th>
								<th>Progress</th>
								<th>Scheduler</th>
								<th>Recovery</th>
							</tr>
						</thead>
						<tbody>
							{#each queueRows as row (row.key)}
								<tr class:job-row--blocked={row.tone === 'fail'}>
									<td><StateBadge compact tone={row.tone} label={row.status} /></td>
									<td>
										{#if canOpenFolder(row)}
											<a class="work-link" href={resolve(folderRoutePath(row.prefix))}>
												<strong>{row.prefix}</strong>
												<span>{row.kind} · {row.phase}</span>
											</a>
										{:else}
											<div class="work-link">
												<strong>{row.prefix}</strong>
												<span>{row.kind} · {row.phase}</span>
											</div>
										{/if}
									</td>
									<td>{row.host}</td>
									<td>
										<strong>{row.progress}</strong>
										<span>{row.detail}</span>
									</td>
									<td>{row.scheduler}</td>
									<td>
										{#if row.action}
											{@const action = row.action}
											<button
												type="button"
												class="control control--compact"
												disabled={rowActionDisabled(row)}
												title={rowRecoveryTitle(row)}
												onclick={() => runAction(action, undefined, row)}
												>{rowRecoveryLabel(row)}</button
											>
										{:else}
											<span class="disabled-copy">{rowRecoveryLabel(row)}</span>
										{/if}
									</td>
								</tr>
							{:else}
								<tr>
									<td colspan="6">
										No active encode, sample, or proof jobs are visible. Scheduler controls remain
										available when runtime data is present.
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			</WorkstationPanel>
		</section>

		<aside class="ops__rail" aria-label="Host readiness">
			<WorkstationPanel
				eyebrow="Hosts"
				title="Readiness"
				meta={`${readyHosts.toLocaleString('en-US')}/${hosts?.hosts.length ?? 0}`}
			>
				<div class="host-list">
					{#each hosts?.hosts ?? [] as host (host.key)}
						<div class="host-row">
							<div class="host-row__head">
								<StateBadge compact tone={hostTone(host)} label={hostStateCopy(host)} />
								<strong>{host.label}</strong>
							</div>
							<dl>
								<dt>Capability</dt>
								<dd>{host.capabilities.join(' · ') || 'none'}</dd>
								<dt>Schedule</dt>
								<dd>{host.schedule_detail || host.schedule_profile_label}</dd>
								<dt>Active</dt>
								<dd>{host.active_encode_count.toLocaleString('en-US')} encodes</dd>
								<dt>Reason</dt>
								<dd>{host.message || host.active_reason || 'ready'}</dd>
							</dl>
							{#if host.setup_requires_password && host.setup_supported}
								<label class="host-password">
									<span>Prepare password</span>
									<input
										type="password"
										autocomplete="current-password"
										value={hostPasswords[host.key] ?? ''}
										placeholder="Required for setup"
										oninput={(event) => handleHostPasswordInput(host, event)}
									/>
								</label>
							{/if}
							<div class="host-row__actions" aria-label={`${host.label} controls`}>
								<button
									type="button"
									class="control control--compact"
									disabled={hostActionDisabled('start-host', host)}
									title={hostActionDisabled('start-host', host)
										? 'Start is unavailable for this host state.'
										: actionTitle('start-host')}
									onclick={() => runAction('start-host', host)}>Start</button
								>
								<button
									type="button"
									class="control control--compact"
									disabled={hostActionDisabled('prepare-host', host)}
									title={hostPrepareTitle(host)}
									onclick={() => runAction('prepare-host', host)}>Prepare</button
								>
								<button
									type="button"
									class="control control--compact"
									disabled={hostActionDisabled('reset-host-trust', host)}
									title={host.trust_reset_supported
										? actionTitle('reset-host-trust')
										: 'Trust reset is unavailable for this host.'}
									onclick={() => runAction('reset-host-trust', host)}>Trust</button
								>
							</div>
						</div>
					{:else}
						<div class="empty-note">Host runtime data is missing.</div>
					{/each}
				</div>
			</WorkstationPanel>

			<WorkstationPanel eyebrow="Schedule" title="Window state">
				<div class="schedule-list">
					<div class="scope-row scope-row--active">
						<span>Policy</span>
						<strong>{encodeQueue?.state.scheduler_summary ?? 'unknown'}</strong>
						<small
							>{queuedWaitingCount.toLocaleString('en-US')} encode jobs waiting on schedule</small
						>
						<a class="inline-link" href={resolve('/settings')}>Edit schedule</a>
					</div>
					{#each closedHosts as host (host.key)}
						<div class="scope-row scope-row--wait">
							<span>{host.label}</span>
							<strong>Closed</strong>
							<small>{host.schedule_detail}</small>
						</div>
					{:else}
						<div class="scope-row">
							<span>Host windows</span>
							<strong>Open or unavailable</strong>
							<small>No scheduled-off host reported by the runtime payload</small>
						</div>
					{/each}
				</div>
			</WorkstationPanel>
		</aside>
	</main>
</OperatorShell>

<style>
	.ops {
		display: grid;
		grid-template-columns: minmax(0, 1fr) var(--mf-workstation-rail-width);
		min-height: calc(100vh - 178px);
	}

	.ops__main {
		align-content: start;
		display: grid;
		gap: var(--mf-space-5);
		min-width: 0;
		padding: var(--mf-space-6);
	}

	.ops__rail {
		align-content: start;
		background: var(--mf-bg-shell);
		border-left: var(--mf-border);
		display: flex;
		flex-direction: column;
		gap: var(--mf-space-5);
		min-width: 0;
		padding: var(--mf-space-5);
	}

	.ops-header {
		align-items: end;
		border-bottom: var(--mf-border);
		display: grid;
		gap: var(--mf-space-6);
		grid-template-columns: minmax(0, 1fr) auto;
		padding-bottom: var(--mf-space-5);
	}

	.ops-header h1 {
		margin-top: var(--mf-space-3);
	}

	.ops-header p {
		margin-top: var(--mf-space-3);
		max-width: 74ch;
	}

	.ops-header__facts {
		display: flex;
		flex-wrap: wrap;
		gap: var(--mf-space-5);
	}

	.ops-header__facts div {
		display: grid;
		gap: var(--mf-space-2);
		min-width: 88px;
	}

	.ops-header__facts span,
	.scope-row span,
	.host-row dt {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-semibold);
		letter-spacing: 0.08em;
		text-transform: uppercase;
	}

	.ops-header__facts strong,
	.host-row dd {
		font-family: var(--mf-font-mono);
		font-size: var(--mf-text-sm);
		font-weight: var(--mf-weight-medium);
	}

	.scheduler-console,
	.blocker-list,
	.host-list,
	.schedule-list {
		display: grid;
		gap: var(--mf-space-4);
		padding: var(--mf-space-5);
	}

	.scheduler-console__state,
	.blocker-row {
		align-items: center;
		display: grid;
		gap: var(--mf-space-4);
		grid-template-columns: auto minmax(0, 1fr) auto;
	}

	.scheduler-console__state > div,
	.blocker-row > div {
		display: grid;
		gap: var(--mf-space-1);
		min-width: 0;
	}

	.scheduler-console__state strong,
	.blocker-row strong,
	.host-row strong,
	.scope-row strong {
		font-size: var(--mf-text-sm);
		font-weight: var(--mf-weight-semibold);
		overflow-wrap: anywhere;
	}

	.scheduler-console__state span,
	.blocker-row span,
	.scope-row small {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-xs);
	}

	.scheduler-console__actions,
	.host-row__actions {
		display: flex;
		flex-wrap: wrap;
		gap: var(--mf-space-3);
	}

	.action-message,
	.action-error {
		border-left: 2px solid var(--mf-ready-fg);
		color: var(--mf-fg-secondary);
		font-size: var(--mf-text-xs);
		padding-left: var(--mf-space-4);
	}

	.action-error {
		border-left-color: var(--mf-fail-fg);
		color: var(--mf-fail-fg);
	}

	.refresh-note {
		color: var(--mf-fg-tertiary);
		font-family: var(--mf-font-mono);
		font-size: var(--mf-text-2xs);
	}

	.refresh-note--error {
		color: var(--mf-fail-fg);
	}

	.blocker-row {
		background: var(--mf-bg-panel-2);
		border: var(--mf-border-muted);
		border-left: 2px solid var(--mf-line-strong);
		min-height: var(--mf-row-comfy);
		padding: var(--mf-space-4);
	}

	.blocker-row--fail {
		background: var(--mf-fail-bg);
		border-left-color: var(--mf-fail-fg);
	}

	.blocker-row--wait {
		background: var(--mf-wait-bg);
		border-left-color: var(--mf-wait-fg);
	}

	.table-wrap {
		overflow: auto;
	}

	table {
		border-collapse: collapse;
		min-width: 860px;
		width: 100%;
	}

	th,
	td {
		border-bottom: var(--mf-border-muted);
		font-size: var(--mf-text-xs);
		height: var(--mf-row-default);
		padding: var(--mf-space-2) var(--mf-space-5);
		text-align: left;
		vertical-align: middle;
	}

	th {
		background: var(--mf-bg-strip);
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-semibold);
		letter-spacing: 0.08em;
		position: sticky;
		text-transform: uppercase;
		top: 0;
	}

	td:nth-child(3),
	td:nth-child(4),
	td:nth-child(5) {
		font-family: var(--mf-font-mono);
	}

	td:nth-child(4) {
		display: grid;
		gap: var(--mf-space-1);
	}

	td:nth-child(4) span {
		color: var(--mf-fg-tertiary);
		font-family: var(--mf-font-sans);
		overflow-wrap: anywhere;
	}

	.job-row--blocked {
		background: var(--mf-fail-bg);
	}

	.work-link {
		display: grid;
		gap: var(--mf-space-1);
		min-width: 0;
	}

	.work-link strong {
		font-size: var(--mf-text-sm);
		font-weight: var(--mf-weight-semibold);
		overflow-wrap: anywhere;
	}

	.work-link span,
	.disabled-copy,
	.inline-link {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
	}

	.inline-link {
		font-weight: var(--mf-weight-semibold);
		text-transform: uppercase;
	}

	.inline-link:hover {
		color: var(--mf-active-fg);
	}

	.control {
		align-items: center;
		background: var(--mf-bg-panel-2);
		border: var(--mf-border-strong);
		border-radius: var(--mf-radius-1);
		color: var(--mf-fg-primary);
		display: inline-flex;
		font-size: var(--mf-text-xs);
		font-weight: var(--mf-weight-semibold);
		justify-content: center;
		min-height: var(--mf-control-md);
		padding: 0 var(--mf-space-4);
		white-space: nowrap;
	}

	.control:hover:not(:disabled) {
		background: var(--mf-bg-raised);
	}

	.control:disabled {
		border-color: var(--mf-line-muted);
	}

	.control--compact {
		min-height: var(--mf-control-sm);
	}

	.control--ready {
		border-color: var(--mf-ready-line);
		color: var(--mf-ready-fg);
	}

	.control--warn {
		border-color: var(--mf-wait-line);
		color: var(--mf-wait-fg);
	}

	.control--danger {
		border-color: var(--mf-fail-line);
		color: var(--mf-fail-fg);
	}

	.control--armed {
		background: var(--mf-fail-bg-strong);
		border-color: var(--mf-fail-fg);
		color: var(--mf-fail-fg-bright);
	}

	.host-row {
		background: var(--mf-bg-panel-2);
		border: var(--mf-border-muted);
		display: grid;
		gap: var(--mf-space-4);
		padding: var(--mf-space-4);
	}

	.host-row__head {
		align-items: center;
		display: grid;
		gap: var(--mf-space-3);
		grid-template-columns: auto minmax(0, 1fr);
	}

	.host-row dl {
		display: grid;
		grid-template-columns: 72px minmax(0, 1fr);
		row-gap: var(--mf-space-3);
	}

	.host-password {
		display: grid;
		gap: var(--mf-space-2);
	}

	.host-password span {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-semibold);
		letter-spacing: 0.08em;
		text-transform: uppercase;
	}

	.host-password input {
		background: var(--mf-bg-input);
		border: var(--mf-border);
		border-radius: var(--mf-radius-1);
		color: var(--mf-fg-primary);
		font: inherit;
		min-height: var(--mf-control-md);
		min-width: 0;
		padding: 0 var(--mf-space-3);
		width: 100%;
	}

	.host-row dd {
		overflow-wrap: anywhere;
	}

	.scope-row {
		border-left: 2px solid var(--mf-line-strong);
		display: grid;
		gap: var(--mf-space-2);
		padding: var(--mf-space-4);
	}

	.scope-row--active {
		background: var(--mf-active-bg);
		border-left-color: var(--mf-active-fg);
	}

	.scope-row--wait {
		background: var(--mf-wait-bg);
		border-left-color: var(--mf-wait-fg);
	}

	.empty-note {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-sm);
		padding: var(--mf-space-5);
	}

	@media (max-width: 980px) {
		.ops {
			grid-template-columns: 1fr;
		}

		.ops__rail {
			border-left: 0;
			border-top: var(--mf-border);
		}

		.ops-header {
			grid-template-columns: 1fr;
		}
	}

	@media (max-width: 680px) {
		.scheduler-console__state,
		.blocker-row {
			align-items: start;
			grid-template-columns: 1fr;
		}
	}
</style>
