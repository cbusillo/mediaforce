<script lang="ts">
	import { onMount } from 'svelte';
	import { resolve } from '$app/paths';
	import { postJson } from '$lib/api/client';
	import type {
		DashboardSummaryPayload,
		HostRuntime,
		HostsPayload,
		OperatorWorkPayload
	} from '$lib/api/types';
	import { folderRoutePath } from '$lib/folder-display';
	import { hostSchedulePresentation, workScheduleSummaryCopy } from '$lib/hosts/schedule';
	import OperatorWorkConsole from './OperatorWorkConsole.svelte';
	import OperatorShell from './OperatorShell.svelte';
	import StateBadge from './StateBadge.svelte';
	import WorkstationPanel from './WorkstationPanel.svelte';
	import {
		activityScheduleDetailCopy,
		activitySchedulePresentationCopy,
		buildOpsBlockers,
		buildOpsFooterSignals,
		buildOpsHistoryRows,
		buildOpsQueueRows,
		buildOpsReadinessSummary,
		buildOpsStatusTiles,
		hostPrepareDisabled,
		hostPrepareTitle,
		hostStateCopy,
		hostTone,
		hostWorkReason,
		opsWorkLabel,
		rowRecoveryLabel,
		rowRecoveryTitle,
		RefreshCoordinator,
		workerCapabilitiesSummary,
		type OpsActionId,
		type OpsQueueRow
	} from './ops-workstation';
	import { operatorRefreshInterval, type OperatorWorkQuery } from './operator-work';

	let {
		dashboard,
		hosts,
		operatorWork,
		loadError,
		onRefresh = async () => {},
		onOperatorRefresh = async () => {}
	}: {
		dashboard: DashboardSummaryPayload | null;
		hosts: HostsPayload | null;
		operatorWork: OperatorWorkPayload | null;
		loadError: string | null;
		onRefresh?: () => Promise<void>;
		onOperatorRefresh?: (query?: OperatorWorkQuery) => Promise<void>;
	} = $props();

	const queueRows = $derived(buildOpsQueueRows(dashboard, hosts));
	const historyRows = $derived(buildOpsHistoryRows(dashboard));
	const blockers = $derived(buildOpsBlockers(dashboard, hosts, loadError));
	const readiness = $derived(buildOpsReadinessSummary(dashboard, hosts, loadError));
	const statusTiles = $derived(buildOpsStatusTiles(dashboard, hosts, loadError));
	const footerSignals = $derived(buildOpsFooterSignals(dashboard, hosts));
	const encodeQueue = $derived(dashboard?.encode_queue ?? null);
	const calibrationQueue = $derived(dashboard?.calibration_queue ?? null);
	const queuedWaitingCount = $derived(encodeQueue?.queued_waiting_count ?? 0);
	const needsAttentionCount = $derived(encodeQueue?.needs_attention_count ?? 0);
	const encodeWorkCount = $derived(
		(encodeQueue?.running_count ?? 0) + (encodeQueue?.queued_count ?? 0)
	);
	const readyHosts = $derived(hosts?.hosts.filter((host) => host.available).length ?? 0);
	const fleetHasReadyCapacity = $derived(readyHosts > 0);
	const notableScheduleHosts = $derived(
		(hosts?.hosts ?? [])
			.map((host) => ({
				host,
				schedule: activitySchedulePresentationCopy(hostSchedulePresentation(host, encodeQueue), [
					host.key,
					host.host ?? '',
					host.label
				])
			}))
			.filter(
				(entry) =>
					entry.schedule &&
					['host_off_schedule', 'host_draining', 'bypassed'].includes(entry.schedule.state)
			)
	);
	const liveRefreshInterval = $derived(
		operatorRefreshInterval(operatorWork) ??
			(!encodeQueue?.state.is_paused &&
			!encodeQueue?.state.stop_requested &&
			(encodeWorkCount > 0 || (calibrationQueue?.active_count ?? 0) > 0)
				? 5_000
				: null)
	);

	let actionPending = $state<OpsActionId | null>(null);
	let confirmationAction = $state<OpsActionId | null>(null);
	let actionMessage = $state('');
	let actionError = $state('');
	const refreshCoordinator = new RefreshCoordinator();
	let manualRefreshPending = $state(false);
	let refreshError = $state('');
	let lastRefreshAt = $state<Date | null>(null);
	let hostPasswords = $state<Record<string, string>>({});

	const globalCommands = $derived([
		{
			id: 'pause-encode' as const,
			tone: 'default',
			enabled:
				Boolean(encodeQueue) &&
				!encodeQueue?.state.is_paused &&
				!encodeQueue?.state.stop_requested &&
				encodeWorkCount > 0 &&
				actionPending === null,
			unavailable: 'No media work is running or waiting.'
		},
		{
			id: 'resume-encode' as const,
			tone: 'ready',
			enabled:
				Boolean(encodeQueue) &&
				Boolean(encodeQueue?.state.is_paused || encodeQueue?.state.stop_requested) &&
				actionPending === null,
			unavailable: 'Work is already accepting eligible folders.'
		},
		{
			id: 'retry-failed-encode' as const,
			tone: 'warn',
			enabled: Boolean(encodeQueue) && needsAttentionCount > 0 && actionPending === null,
			unavailable: 'No approved processing retries are waiting.'
		},
		{
			id: 'stop-encode' as const,
			tone: 'danger',
			enabled:
				Boolean(encodeQueue) &&
				Boolean(
					(encodeQueue?.running_count ?? 0) > 0 ||
					(encodeQueue?.queued_count ?? 0) > 0 ||
					encodeQueue?.state.is_paused
				) &&
				actionPending === null,
			unavailable: 'No media work is running, waiting, or paused.'
		},
		{
			id: 'stop-calibration' as const,
			tone: 'danger',
			enabled:
				Boolean(calibrationQueue) &&
				(calibrationQueue?.active_count ?? 0) > 0 &&
				actionPending === null,
			unavailable: 'No sample or review jobs are running.'
		}
	]);
	const availableGlobalCommands = $derived(globalCommands.filter((command) => command.enabled));
	const workControlCommands = $derived(
		availableGlobalCommands.filter((command) => command.id !== 'retry-failed-encode')
	);
	const unavailableGlobalCommands = $derived(
		globalCommands.filter((command) => !command.enabled && actionPending === null)
	);

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
		if (action === 'stop-encode') return 'Stop current media work and pause what is waiting';
		if (action === 'stop-calibration') return 'Stop samples that are running or waiting';
		if (action === 'retry-failed-encode')
			return 'Retry approved folders that are ready to process again';
		if (action === 'retry-encode-prefix') return 'Retry processing for this folder';
		if (action === 'pause-encode') return 'Pause processing';
		if (action === 'resume-encode') return 'Resume processing and clear stop request';
		if (action === 'start-host') return 'Start or wake this computer';
		if (action === 'prepare-host') return 'Set up this computer for Mediaforce work';
		return 'Reset stored trust for this computer';
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
			await onRefresh();
		} catch (error) {
			actionError = error instanceof Error ? error.message : 'Action failed.';
		} finally {
			actionPending = null;
		}
	}

	async function refreshOps({ quiet = false }: { quiet?: boolean } = {}) {
		const refreshKind = quiet ? 'quiet' : 'manual';
		const refreshId = refreshCoordinator.start(refreshKind);
		if (refreshId === null) return;

		if (!quiet) {
			manualRefreshPending = true;
			actionMessage = '';
			actionError = '';
		}

		refreshError = '';
		try {
			await onRefresh();
			if (!refreshCoordinator.finish(refreshKind, refreshId)) return;
			lastRefreshAt = new Date();
			if (!quiet) actionMessage = 'Activity refreshed.';
		} catch (error) {
			if (!refreshCoordinator.finish(refreshKind, refreshId)) return;
			refreshError = error instanceof Error ? error.message : 'Refresh failed.';
			if (!quiet) actionError = refreshError;
		} finally {
			if (!quiet) manualRefreshPending = false;
		}
	}

	function refreshCopy(): string {
		if (manualRefreshPending) return 'refreshing';
		if (!lastRefreshAt)
			return liveRefreshInterval ? 'watching active work' : 'manual refresh while idle';
		const refreshedAt = lastRefreshAt.toLocaleTimeString([], {
			hour: '2-digit',
			minute: '2-digit',
			second: '2-digit'
		});
		return `refreshed ${refreshedAt} · ${liveRefreshInterval ? 'watching active work' : 'quiet while idle'}`;
	}

	function rowActionDisabled(row: OpsQueueRow): boolean {
		return actionPending !== null || !row.action;
	}

	function hostActionDisabled(action: OpsActionId, host: HostRuntime): boolean {
		if (actionPending !== null) return true;
		if (action === 'start-host') return host.available || !host.setup_supported;
		if (action === 'prepare-host') {
			if (host.available && !host.setup_requires_password && host.issues.length === 0) return true;
			return hostPrepareDisabled(host, hostPasswords[host.key] ?? '');
		}
		if (action === 'reset-host-trust') return !host.trust_reset_supported;
		return false;
	}

	function hostHasVisibleActions(host: HostRuntime): boolean {
		return (
			!hostActionDisabled('start-host', host) ||
			!hostActionDisabled('prepare-host', host) ||
			!hostActionDisabled('reset-host-trust', host) ||
			Boolean(host.setup_supported && host.setup_requires_password)
		);
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
		if (action === 'retry-failed-encode') return 'Retry all waiting work';
		if (action === 'retry-encode-prefix') return 'Retry item';
		if (action === 'stop-encode') return 'Stop processing';
		if (action === 'stop-calibration') return 'Stop samples';
		if (action === 'start-host') return 'Start computer';
		if (action === 'prepare-host') return 'Set up computer';
		if (action === 'reset-host-trust') return 'Reset computer trust';
		return 'Run';
	}

	function canOpenFolder(row: OpsQueueRow): boolean {
		return row.prefix !== 'system scope';
	}

	function queueKindLabel(row: OpsQueueRow): string {
		if (row.kind === 'encode') return row.scopeLabel ?? 'Media';
		if (row.kind === 'proof') return 'Comparison clips';
		return 'Sample';
	}

	onMount(() => {
		lastRefreshAt = new Date();
	});

	$effect(() => {
		const interval = liveRefreshInterval;
		if (!interval) return;
		const timer = setInterval(() => void refreshOps({ quiet: true }), interval);
		return () => clearInterval(timer);
	});
</script>

<OperatorShell route="ops" subject="Activity" crumb="/ops" {statusTiles} {footerSignals}>
	<main class="ops">
		<section class="ops__main" aria-label="Mediaforce activity">
			<h1 class="ops__title">Activity</h1>
			<WorkstationPanel
				eyebrow="Current work"
				title="Working now"
				meta={queueRows.length ? `${queueRows.length.toLocaleString('en-US')} current` : undefined}
			>
				<div class="queue-surface">
					<div class="queue-toolbar">
						<div class="queue-toolbar__state">
							<StateBadge tone={readiness.tone} label={readiness.title} />
							<span
								>{queueRows.length
									? 'Live queue and current media work.'
									: 'New work will appear here when it starts.'}</span
							>
						</div>
						<div class="queue-toolbar__refresh">
							<span class="refresh-note" class:refresh-note--error={Boolean(refreshError)}>
								{refreshError || refreshCopy()}
							</span>
							<button
								type="button"
								class="control control--compact"
								disabled={manualRefreshPending}
								aria-busy={manualRefreshPending}
								title="Refresh activity now"
								onclick={() => refreshOps()}>Refresh</button
							>
						</div>
					</div>

					{#if blockers.length > 0}
						<div class="blocker-list" aria-label="Needs attention">
							{#each blockers as blocker (blocker.key)}
								<div class="blocker-row blocker-row--{blocker.tone}">
									<StateBadge
										tone={blocker.tone}
										label={blocker.tone === 'fail' ? 'Problem' : 'Needs you'}
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
									{:else if blocker.href}
										<a class="control" href={resolve(blocker.href)}>{blocker.linkLabel ?? 'Open'}</a
										>
									{/if}
								</div>
							{/each}
						</div>
					{/if}

					{#if workControlCommands.length > 0 || actionPending || actionMessage || actionError}
						<div class="scheduler-console">
							<div class="scheduler-console__heading">
								<strong>Queue controls</strong>
								<span>Only actions that make sense for the current work are shown.</span>
							</div>
							<div class="scheduler-console__actions" aria-label="Work controls">
								{#each workControlCommands as command (command.id)}
									<button
										type="button"
										class="control"
										class:control--ready={command.tone === 'ready'}
										class:control--warn={command.tone === 'warn'}
										class:control--danger={command.tone === 'danger'}
										class:control--armed={confirmationAction === command.id}
										title={actionTitle(command.id)}
										onclick={() => runAction(command.id)}>{queueActionLabel(command.id)}</button
									>
								{:else}
									<div class="command-standby">
										<StateBadge compact tone="ready" label="No command" />
										<span>Mediaforce does not need a command right now.</span>
									</div>
								{/each}
							</div>
							{#if unavailableGlobalCommands.length > 0}
								<details class="command-details">
									<summary>{unavailableGlobalCommands.length} unavailable controls</summary>
									<ul>
										{#each unavailableGlobalCommands as command (command.id)}
											<li>
												<strong>{queueActionLabel(command.id)}</strong>
												<span>{command.unavailable}</span>
											</li>
										{/each}
									</ul>
								</details>
							{/if}
							{#if actionMessage}
								<p class="action-message">{actionMessage}</p>
							{/if}
							{#if actionError}
								<p class="action-error">{actionError}</p>
							{/if}
						</div>
					{/if}

					{#if queueRows.length > 0}
						<div class="table-wrap">
							<table class="ops-table ops-table--jobs">
								<thead>
									<tr>
										<th>State</th>
										<th>Work</th>
										<th>Computer</th>
										<th>Progress</th>
										<th>Work window</th>
										<th>Next step</th>
									</tr>
								</thead>
								<tbody>
									{#each queueRows as row (row.key)}
										<tr class:job-row--blocked={row.tone === 'fail'}>
											<td data-label="State"
												><StateBadge compact tone={row.tone} label={row.status} /></td
											>
											<td data-label="Work">
												{#if canOpenFolder(row)}
													<a class="work-link" href={resolve(folderRoutePath(row.prefix))}>
														<strong>{opsWorkLabel(row.prefix)}</strong>
														<span>{queueKindLabel(row)} · {row.phase}</span>
													</a>
												{:else}
													<div class="work-link">
														<strong>{opsWorkLabel(row.prefix)}</strong>
														<span>{queueKindLabel(row)} · {row.phase}</span>
													</div>
												{/if}
											</td>
											<td data-label="Computer">{row.host}</td>
											<td data-label="Progress">
												<div class="cell-stack">
													<strong>{row.progress}</strong>
													<span title={row.detail}>{row.detail}</span>
												</div>
											</td>
											<td data-label="Work window" class="schedule-cell">
												<div class="cell-stack schedule-cell__content">
													<StateBadge compact tone={row.schedulerTone} label={row.scheduler} />
													<span>{row.schedulerDetail}</span>
													{#if row.scheduleState === 'draining_impossible'}
														<a class="inline-link" href={resolve('/settings')}>Edit work windows</a>
													{/if}
												</div>
											</td>
											<td data-label="Next step">
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
												{:else if row.status === 'Needs review' && canOpenFolder(row)}
													<a class="inline-link" href={resolve(folderRoutePath(row.prefix))}
														>Review item</a
													>
												{:else}
													<span class="disabled-copy">{rowRecoveryLabel(row)}</span>
												{/if}
											</td>
										</tr>
									{/each}
								</tbody>
							</table>
						</div>
					{:else}
						<div class="current-standby">
							<StateBadge tone="ready" label="No current work" />
							<div>
								<strong>Nothing is running.</strong>
								<span>New work will appear here when it starts.</span>
							</div>
						</div>
					{/if}
				</div>
			</WorkstationPanel>

			{#if historyRows.length > 0}
				<WorkstationPanel
					eyebrow="History"
					title="Recently finished"
					meta={`${historyRows.length.toLocaleString('en-US')} recent`}
				>
					<details class="history-disclosure">
						<summary>
							<StateBadge compact tone="idle" label="History" />
							<span>Show recent sample and review results.</span>
						</summary>
						<div class="table-wrap table-wrap--history">
							<table class="ops-table ops-table--history">
								<thead>
									<tr>
										<th>State</th>
										<th>Work</th>
										<th>Computer</th>
										<th>Last note</th>
										<th>When</th>
									</tr>
								</thead>
								<tbody>
									{#each historyRows as row (row.key)}
										<tr class="job-row--history">
											<td data-label="State"
												><StateBadge compact tone={row.tone} label={row.status} /></td
											>
											<td data-label="Work">
												{#if canOpenFolder(row)}
													<a class="work-link" href={resolve(folderRoutePath(row.prefix))}>
														<strong>{opsWorkLabel(row.prefix)}</strong>
														<span>{queueKindLabel(row)} · {row.phase}</span>
													</a>
												{:else}
													<div class="work-link">
														<strong>{opsWorkLabel(row.prefix)}</strong>
														<span>{queueKindLabel(row)} · {row.phase}</span>
													</div>
												{/if}
											</td>
											<td data-label="Computer">{row.host}</td>
											<td data-label="Last note">
												<strong>{row.progress}</strong>
												<span>{row.detail}</span>
											</td>
											<td data-label="When">{row.scheduler}</td>
										</tr>
									{/each}
								</tbody>
							</table>
						</div>
					</details>
				</WorkstationPanel>
			{/if}

			<OperatorWorkConsole work={operatorWork} onRefresh={onOperatorRefresh} />
		</section>

		<aside class="ops__rail" aria-label="Computer readiness">
			<details class="system-details">
				<summary class="system-details__summary">
					<div>
						<span class="mf-eyebrow">System details</span>
						<strong>Computers and schedule</strong>
					</div>
					<div class="system-details__state">
						<StateBadge
							compact
							tone={readyHosts > 0 ? 'ready' : 'wait'}
							label={`${readyHosts.toLocaleString('en-US')} ready`}
						/>
						<span>Open</span>
					</div>
				</summary>
				<div class="system-details__content">
					<WorkstationPanel
						eyebrow="Computers"
						title="Available computers"
						meta={`${readyHosts.toLocaleString('en-US')}/${hosts?.hosts.length ?? 0}`}
					>
						<div class="host-list">
							{#each hosts?.hosts ?? [] as host (host.key)}
								{@const schedule = activitySchedulePresentationCopy(
									hostSchedulePresentation(host, encodeQueue),
									[host.key, host.host ?? '', host.label]
								)}
								<div class="host-row host-row--{hostTone(host, fleetHasReadyCapacity, dashboard)}">
									<div class="host-row__head">
										<StateBadge
											compact
											tone={hostTone(host, fleetHasReadyCapacity, dashboard)}
											label={hostStateCopy(host, dashboard)}
										/>
										<strong>{host.label}</strong>
									</div>
									<dl>
										<dt>Work window</dt>
										<dd title={activityScheduleDetailCopy(host.schedule_detail)}>
											{schedule?.detail || host.schedule_profile_label || 'Always available'}
										</dd>
										<dt>Now</dt>
										<dd>
											{host.active_encode_count > 0
												? `${host.active_encode_count.toLocaleString('en-US')} compressing ${host.active_encode_count === 1 ? 'task' : 'tasks'}`
												: 'Idle'}
										</dd>
										<dt>Work it can run</dt>
										<dd>{workerCapabilitiesSummary(host.capabilities)}</dd>
									</dl>
									{#if host.setup_requires_password && host.setup_supported}
										<label class="host-password">
											<span>Setup password</span>
											<input
												type="password"
												autocomplete="current-password"
												value={hostPasswords[host.key] ?? ''}
												placeholder="Required for setup"
												oninput={(event) => handleHostPasswordInput(host, event)}
											/>
										</label>
									{/if}
									{#if hostHasVisibleActions(host)}
										<div class="host-row__actions" aria-label={`${host.label} controls`}>
											{#if !hostActionDisabled('start-host', host)}
												<button
													type="button"
													class="control control--compact"
													title={actionTitle('start-host')}
													onclick={() => runAction('start-host', host)}>Start computer</button
												>
											{/if}
											{#if !hostActionDisabled('prepare-host', host) || (host.setup_supported && host.setup_requires_password)}
												<button
													type="button"
													class="control control--compact"
													disabled={hostActionDisabled('prepare-host', host)}
													title={hostPrepareTitle(host)}
													onclick={() => runAction('prepare-host', host)}>Set up computer</button
												>
											{/if}
											{#if !hostActionDisabled('reset-host-trust', host)}
												<button
													type="button"
													class="control control--compact"
													title={actionTitle('reset-host-trust')}
													onclick={() => runAction('reset-host-trust', host)}
													>Reset computer trust</button
												>
											{/if}
										</div>
									{/if}
									<p class="host-row__reason">{hostWorkReason(host, hosts, dashboard)}</p>
								</div>
							{:else}
								<div class="empty-note">Computer status is unavailable.</div>
							{/each}
						</div>
					</WorkstationPanel>

					<WorkstationPanel eyebrow="Work schedule" title="When work may run">
						<div class="schedule-list">
							<div class="scope-row scope-row--active">
								<span>Current schedule</span>
								<strong>
									{workScheduleSummaryCopy(encodeQueue?.state.scheduler_summary) ||
										'Work schedule is unavailable'}
								</strong>
								<small>
									{queuedWaitingCount.toLocaleString('en-US')}
									{queuedWaitingCount === 1 ? 'item' : 'items'} waiting for the next work window
								</small>
								<a class="inline-link" href={resolve('/settings')}>Edit work schedule</a>
							</div>
							{#each notableScheduleHosts as entry (entry.host.key)}
								<div class="scope-row scope-row--wait">
									<span>{entry.host.label}</span>
									<strong>{entry.schedule?.label}</strong>
									<small>{entry.schedule?.detail}</small>
								</div>
							{:else}
								<div class="scope-row">
									<span>Computer schedules</span>
									<strong>Available now or not reported</strong>
									<small>No computer is currently scheduled off</small>
								</div>
							{/each}
						</div>
					</WorkstationPanel>
				</div>
			</details>
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

	.ops__title {
		clip: rect(0 0 0 0);
		clip-path: inset(50%);
		height: 1px;
		overflow: hidden;
		position: absolute;
		white-space: nowrap;
		width: 1px;
	}

	.scope-row span,
	.host-row dt {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-semibold);
		letter-spacing: 0.08em;
		text-transform: uppercase;
	}

	.host-row dd {
		color: var(--mf-fg-secondary);
		font-size: var(--mf-text-xs);
		font-weight: var(--mf-weight-medium);
		line-height: var(--mf-leading-snug);
	}

	.blocker-list,
	.scheduler-console,
	.current-standby,
	.host-list,
	.schedule-list,
	.history-disclosure {
		display: grid;
		gap: var(--mf-space-4);
		padding: var(--mf-space-5);
	}

	.queue-toolbar,
	.blocker-row,
	.current-standby {
		align-items: center;
		display: grid;
		gap: var(--mf-space-4);
		grid-template-columns: auto minmax(0, 1fr) auto;
	}

	.queue-toolbar__state,
	.blocker-row > div,
	.current-standby > div {
		display: grid;
		gap: var(--mf-space-1);
		min-width: 0;
	}

	.scheduler-console__heading strong,
	.blocker-row strong,
	.current-standby strong,
	.host-row strong,
	.scope-row strong {
		font-size: var(--mf-text-sm);
		font-weight: var(--mf-weight-semibold);
		overflow-wrap: anywhere;
	}

	.queue-toolbar__state span,
	.scheduler-console__heading span,
	.blocker-row span,
	.current-standby span,
	.history-disclosure summary span,
	.scope-row small {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-xs);
	}

	.queue-toolbar {
		background: var(--mf-bg-strip);
		border-bottom: var(--mf-border-muted);
		padding: var(--mf-space-4) var(--mf-space-5);
	}

	.queue-toolbar__refresh {
		align-items: center;
		display: flex;
		flex-wrap: wrap;
		gap: var(--mf-space-3);
		justify-content: end;
	}

	.scheduler-console__actions,
	.host-row__actions {
		display: flex;
		flex-wrap: wrap;
		gap: var(--mf-space-3);
	}

	.command-standby {
		align-items: center;
		background: var(--mf-ready-bg);
		border-left: 2px solid var(--mf-ready-fg);
		display: grid;
		gap: var(--mf-space-3);
		grid-template-columns: auto minmax(0, 1fr);
		padding: var(--mf-space-3) var(--mf-space-4);
	}

	.command-standby span {
		color: var(--mf-fg-secondary);
		font-size: var(--mf-text-xs);
	}

	.command-details {
		border-top: var(--mf-border-muted);
		padding-top: var(--mf-space-3);
	}

	.command-details summary {
		color: var(--mf-fg-tertiary);
		cursor: pointer;
		font-size: var(--mf-text-2xs);
		font-weight: var(--mf-weight-semibold);
		letter-spacing: 0.08em;
		text-transform: uppercase;
	}

	.command-details ul {
		display: grid;
		gap: var(--mf-space-2);
		list-style: none;
		margin: var(--mf-space-3) 0 0;
		padding: 0;
	}

	.command-details li {
		display: grid;
		gap: var(--mf-space-1);
		grid-template-columns: minmax(112px, 0.32fr) minmax(0, 1fr);
	}

	.command-details li strong,
	.command-details li span {
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-xs);
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
		font-family: var(--mf-font-mono), monospace;
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

	.scheduler-console__heading {
		background: var(--mf-bg-strip);
		border-left: 2px solid var(--mf-wait-fg);
		display: grid;
		gap: var(--mf-space-1);
		padding: var(--mf-space-4);
	}

	.current-standby {
		background: var(--mf-bg-strip);
		border-left: 2px solid var(--mf-ready-fg);
		grid-template-columns: auto minmax(0, 1fr);
		min-height: var(--mf-row-comfy);
	}

	.history-disclosure {
		gap: 0;
	}

	.history-disclosure summary {
		align-items: center;
		cursor: pointer;
		display: grid;
		gap: var(--mf-space-4);
		grid-template-columns: auto minmax(0, 1fr);
		list-style: none;
	}

	.history-disclosure summary::-webkit-details-marker {
		display: none;
	}

	.history-disclosure[open] summary {
		border-bottom: var(--mf-border-muted);
		padding-bottom: var(--mf-space-4);
	}

	.history-disclosure[open] .table-wrap {
		margin-top: var(--mf-space-4);
	}

	.history-disclosure:not([open]) .table-wrap {
		display: none;
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

	.ops-table--jobs {
		min-width: 720px;
		table-layout: fixed;
	}

	.ops-table--jobs th,
	.ops-table--jobs td {
		padding-inline: var(--mf-space-3);
	}

	.ops-table--jobs td {
		height: auto;
		padding-block: var(--mf-space-3);
	}

	.ops-table--jobs th:nth-child(1) {
		width: 16%;
	}

	.ops-table--jobs th:nth-child(2) {
		width: 23%;
	}

	.ops-table--jobs th:nth-child(3) {
		width: 12%;
	}

	.ops-table--jobs th:nth-child(4) {
		width: 20%;
	}

	.ops-table--jobs th:nth-child(5) {
		width: 15%;
	}

	.ops-table--jobs th:nth-child(6) {
		width: 14%;
	}

	.cell-stack {
		display: grid;
		gap: var(--mf-space-2);
		justify-items: start;
		min-width: 0;
	}

	.ops-table--jobs .work-link strong,
	.ops-table--jobs .cell-stack > span {
		overflow-wrap: anywhere;
	}

	.ops-table--jobs .cell-stack > span {
		display: -webkit-box;
		line-clamp: 3;
		overflow: hidden;
		-webkit-box-orient: vertical;
		-webkit-line-clamp: 3;
	}

	.schedule-cell__content > span {
		color: var(--mf-fg-secondary);
		display: block;
		line-height: 1.35;
	}

	.schedule-cell__content .inline-link {
		display: inline-block;
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
		font-family: var(--mf-font-mono), monospace;
	}

	td:nth-child(4) {
		display: grid;
		gap: var(--mf-space-1);
	}

	td:nth-child(4) span {
		color: var(--mf-fg-tertiary);
		font-family: var(--mf-font-sans), sans-serif;
		overflow-wrap: anywhere;
	}

	.job-row--blocked {
		background: var(--mf-fail-bg);
	}

	.job-row--history {
		color: var(--mf-fg-secondary);
	}

	.job-row--history .work-link strong {
		color: var(--mf-fg-secondary);
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
		border-left: 2px solid var(--mf-line-strong);
		display: grid;
		gap: var(--mf-space-4);
		padding: var(--mf-space-4);
	}

	.host-row--active {
		background: var(--mf-active-bg);
		border-left-color: var(--mf-active-fg);
	}

	.host-row--ready {
		background: var(--mf-ready-bg);
		border-left-color: var(--mf-ready-fg);
	}

	.host-row--fail {
		background: var(--mf-fail-bg);
		border-left-color: var(--mf-fail-fg);
	}

	.host-row--wait {
		background: var(--mf-bg-strip);
		border-left-color: var(--mf-wait-fg);
	}

	.host-row--idle {
		background: var(--mf-bg-strip);
		opacity: 0.86;
	}

	.host-row__head {
		align-items: center;
		display: grid;
		gap: var(--mf-space-3);
		grid-template-columns: auto minmax(0, 1fr);
	}

	.host-row dl {
		column-gap: var(--mf-space-4);
		display: grid;
		grid-template-columns: 72px minmax(0, 1fr);
		row-gap: var(--mf-space-3);
	}

	.host-row dt,
	.host-row dd {
		margin: 0;
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

	.host-row__reason {
		border-left: 2px solid var(--mf-line-strong);
		color: var(--mf-fg-tertiary);
		font-size: var(--mf-text-xs);
		line-height: 1.45;
		margin: 0;
		padding-left: var(--mf-space-3);
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

	@media (max-width: 720px) {
		.table-wrap {
			overflow: visible;
		}

		.ops-table {
			border-collapse: separate;
			border-spacing: 0;
			min-width: 0;
		}

		.ops-table thead {
			display: none;
		}

		.ops-table tbody,
		.ops-table tr,
		.ops-table td {
			display: block;
			width: 100%;
		}

		.ops-table tr {
			background: var(--mf-bg-panel-2);
			border: var(--mf-border-muted);
			display: grid;
			margin-bottom: var(--mf-space-3);
		}

		.ops-table td {
			align-items: start;
			border-bottom: var(--mf-border-muted);
			display: grid;
			gap: var(--mf-space-3);
			grid-template-columns: minmax(72px, 0.32fr) minmax(0, 1fr);
			height: auto;
			min-height: var(--mf-control-md);
			padding: var(--mf-space-3);
		}

		.ops-table td:last-child {
			border-bottom: 0;
		}

		.ops-table td::before {
			color: var(--mf-fg-tertiary);
			content: attr(data-label);
			font-family: var(--mf-font-sans), sans-serif;
			font-size: var(--mf-text-2xs);
			font-weight: var(--mf-weight-semibold);
			text-transform: uppercase;
		}

		.ops-table td:nth-child(4) {
			display: grid;
		}

		.ops-table .control {
			justify-self: start;
			max-width: 100%;
			white-space: normal;
		}
	}

	@media (max-width: 980px) {
		.ops {
			grid-template-columns: 1fr;
		}

		.ops__rail {
			border-left: 0;
			border-top: var(--mf-border);
		}
	}

	@media (max-width: 680px) {
		.queue-toolbar,
		.blocker-row,
		.current-standby {
			align-items: start;
			grid-template-columns: 1fr;
		}

		.queue-toolbar__refresh {
			justify-content: start;
		}

		.command-details li {
			grid-template-columns: 1fr;
		}
	}

	/* Human activity surface */
	.ops {
		display: grid;
		gap: 18px;
		grid-template-columns: minmax(0, 1fr) 296px;
		margin: 0 auto;
		max-width: 1088px;
		min-height: 0;
		padding: 34px 24px 64px;
	}

	.ops__main {
		gap: 16px;
		padding: 0;
	}

	.ops__rail {
		background: transparent;
		border: 0;
		gap: 16px;
		padding: 0;
	}

	.system-details {
		min-width: 0;
		width: 100%;
	}

	.system-details__summary {
		align-items: center;
		background: var(--mf-bg-panel);
		border: 1px solid var(--mf-line);
		border-radius: var(--mf-radius-3);
		cursor: pointer;
		display: flex;
		gap: 12px;
		justify-content: space-between;
		list-style: none;
		padding: 12px;
	}

	.system-details__summary::-webkit-details-marker {
		display: none;
	}

	.system-details__summary > div:first-child {
		display: grid;
		gap: 7px;
		min-width: 0;
	}

	.system-details__summary strong {
		color: var(--mf-fg-primary);
		font-size: 14px;
	}

	.system-details__state {
		align-items: flex-end;
		display: grid;
		gap: 5px;
		justify-items: end;
	}

	.system-details__state > span {
		color: var(--mf-fg-tertiary);
		font-size: 11px;
	}

	.system-details__content {
		display: grid;
		gap: 16px;
		padding-top: 12px;
	}

	:global(.ops .mf-eyebrow) {
		background: var(--mf-active-bg);
		border-radius: 999px;
		color: var(--mf-active-fg);
		font-family: var(--mf-font-sans);
		font-size: 11px;
		font-weight: 700;
		letter-spacing: 0.02em;
		padding: 6px 9px;
		text-transform: none;
		width: fit-content;
	}

	.blocker-list,
	.scheduler-console,
	.host-list,
	.schedule-list {
		gap: 8px;
		padding: 12px;
	}

	.blocker-row,
	.scheduler-console__heading,
	.command-standby,
	.host-row,
	.scope-row,
	.empty-note,
	.current-standby {
		background: var(--mf-bg-panel-2);
		border: 1px solid var(--mf-line-muted);
		border-radius: var(--mf-radius-2);
		box-shadow: none;
		color: var(--mf-fg-primary);
	}

	.blocker-row {
		grid-template-columns: auto minmax(0, 1fr) auto;
		padding: 12px;
	}

	.blocker-row::before,
	.host-row::before,
	.scope-row::before {
		display: none;
	}

	.blocker-row strong,
	.scheduler-console strong,
	.host-row strong,
	.scope-row strong,
	.current-standby strong,
	.empty-note strong {
		color: var(--mf-fg-primary);
		font-family: var(--mf-font-sans);
	}

	.blocker-row span,
	.scheduler-console span,
	.host-row span,
	.scope-row span,
	.scope-row small,
	.current-standby span,
	.empty-note span {
		color: var(--mf-fg-secondary);
		font-family: var(--mf-font-sans);
	}

	.scheduler-console {
		display: grid;
		grid-template-columns: minmax(0, 1fr);
	}

	.scheduler-console__heading {
		padding: 12px;
	}

	.scheduler-console__actions {
		background: transparent;
		border: 0;
		gap: 8px;
		padding: 0;
	}

	.control {
		background: var(--mf-bg-panel);
		border: 1px solid var(--mf-line-strong);
		border-radius: var(--mf-radius-2);
		color: var(--mf-fg-primary);
		font-family: var(--mf-font-sans);
		font-weight: 600;
		min-height: 34px;
		padding: 0 11px;
	}

	.control:hover {
		background: var(--mf-bg-panel-2);
		border-color: var(--mf-active-line);
		color: var(--mf-active-fg);
	}

	.control--ready {
		background: var(--mf-active-solid);
		border-color: var(--mf-active-solid);
		color: var(--mf-fg-on-accent);
	}

	.control--warn {
		background: var(--mf-wait-bg);
		border-color: var(--mf-wait-line);
		color: var(--mf-wait-fg);
	}

	.control--danger,
	.control--armed {
		background: var(--mf-fail-bg);
		border-color: var(--mf-fail-line);
		color: var(--mf-fail-fg);
	}

	.command-details {
		background: transparent;
		border: 0;
		color: var(--mf-fg-secondary);
	}

	.command-details summary,
	.refresh-note,
	.disabled-copy {
		color: var(--mf-fg-tertiary);
		font-family: var(--mf-font-sans);
		font-size: 12px;
	}

	.table-wrap {
		background: var(--mf-bg-panel);
		border: 0;
		overflow-x: auto;
	}

	.ops-table {
		background: var(--mf-bg-panel);
		color: var(--mf-fg-primary);
	}

	.ops-table th {
		background: var(--mf-bg-panel-2);
		border-bottom: 1px solid var(--mf-line);
		color: var(--mf-fg-tertiary);
		font-family: var(--mf-font-sans);
		font-size: 11px;
		letter-spacing: 0.04em;
		text-transform: none;
	}

	.ops-table td {
		border-bottom: 1px solid var(--mf-line-muted);
		color: var(--mf-fg-secondary);
		font-family: var(--mf-font-sans);
		font-size: 13px;
	}

	.work-link strong {
		color: var(--mf-fg-primary);
		font-family: var(--mf-font-sans);
	}

	.work-link span {
		color: var(--mf-fg-tertiary);
		font-family: var(--mf-font-sans);
	}

	.host-row dl {
		border: 0;
		grid-template-columns: 76px minmax(0, 1fr);
	}

	.host-row dt {
		color: var(--mf-fg-tertiary);
		font-family: var(--mf-font-sans);
	}

	.host-row dd {
		color: var(--mf-fg-secondary);
		font-family: var(--mf-font-sans);
	}

	.host-password input {
		background: var(--mf-bg-input);
		border: 1px solid var(--mf-line-strong);
		border-radius: var(--mf-radius-2);
		color: var(--mf-fg-primary);
	}

	.inline-link {
		color: var(--mf-active-fg);
	}

	button:focus-visible,
	a:focus-visible,
	input:focus-visible,
	summary:focus-visible {
		box-shadow: var(--mf-ring-focus);
		outline: none;
	}

	@media (max-width: 1100px) {
		.ops {
			grid-template-columns: 1fr;
		}

		.ops__rail {
			display: block;
		}
	}

	@media (max-width: 680px) {
		.ops {
			padding: 26px 12px 48px;
		}

		.blocker-row {
			grid-template-columns: 1fr;
		}
	}
</style>
