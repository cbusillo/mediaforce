<script lang="ts">
	import { resolve } from '$app/paths';
	import type { CatalogWarningNotice } from '$lib/season/experience';
	import type { RecoveryAlert } from './recovery-alerts';

	let {
		catalogWarning,
		recoveryAlerts,
		retryingAlert,
		retryOutcome,
		onRetry
	}: {
		catalogWarning: CatalogWarningNotice | null;
		recoveryAlerts: RecoveryAlert[];
		retryingAlert: string | null;
		retryOutcome: { id: string; message: string } | null;
		onRetry: (alert: RecoveryAlert) => void;
	} = $props();
</script>

{#if catalogWarning}
	<div class="catalog-warning" role="alert" aria-live="polite">
		<div class="catalog-warning__inner">
			<strong>{catalogWarning.title}</strong>
			<span>{catalogWarning.detail}</span>
		</div>
	</div>
{/if}

{#each recoveryAlerts as alert (alert.id)}
	<div class="catalog-warning" role="alert" aria-live="polite">
		<div class="catalog-warning__inner">
			<strong>{alert.title}</strong>
			<span>{alert.detail} <a href={resolve('/ops')}>View computers</a></span>
			{#if alert.technical}
				<details class="recovery-alert__details">
					<summary>Details</summary>
					<span>{alert.technical}</span>
				</details>
			{/if}
			{#if alert.host_key}
				<button
					type="button"
					class="recovery-alert__retry"
					disabled={retryingAlert !== null}
					aria-busy={retryingAlert === alert.id}
					title="Check this computer and try to reconnect its storage now, without opening any window on it"
					onclick={() => onRetry(alert)}>Retry connection</button
				>
			{/if}
			{#if retryOutcome?.id === alert.id && retryOutcome.message}
				<span class="recovery-alert__outcome" role="status">{retryOutcome.message}</span>
			{/if}
		</div>
	</div>
{/each}

<style>
	.catalog-warning {
		background: var(--mf-wait-bg);
		border-bottom: 1px solid var(--mf-wait-line);
		color: var(--mf-wait-fg);
	}

	.catalog-warning__inner {
		align-items: baseline;
		display: flex;
		font-size: 13px;
		gap: 8px;
		margin: 0 auto;
		max-width: var(--mf-shell-max);
		padding: 9px var(--mf-shell-pad);
	}

	.catalog-warning__inner strong {
		overflow-wrap: anywhere;
	}

	.catalog-warning__inner {
		flex-wrap: wrap;
	}

	.catalog-warning__inner > span:not(.recovery-alert__outcome) {
		flex: 1 1 20rem;
	}

	.recovery-alert__retry {
		align-self: center;
		background: var(--mf-bg-panel);
		border: 1px solid var(--mf-wait-line);
		border-radius: var(--mf-radius-1);
		color: var(--mf-fg-primary);
		cursor: pointer;
		flex: none;
		font: inherit;
		font-size: var(--mf-text-xs);
		font-weight: var(--mf-weight-semibold);
		min-height: 28px;
		padding: 0 var(--mf-space-3);
		white-space: nowrap;
	}

	.recovery-alert__retry:disabled {
		cursor: progress;
		opacity: 0.6;
	}

	.recovery-alert__details {
		flex: none;
		font-size: var(--mf-text-xs);
	}

	.recovery-alert__details summary {
		cursor: pointer;
	}

	.recovery-alert__details span {
		display: block;
		overflow-wrap: anywhere;
	}

	.recovery-alert__outcome {
		flex-basis: 100%;
		font-weight: var(--mf-weight-semibold);
	}

	@media (max-width: 680px) {
		.catalog-warning__inner {
			align-items: stretch;
			flex-direction: column;
			gap: 2px;
			padding: 9px 14px;
		}

		.catalog-warning__inner strong {
			white-space: normal;
		}

		.catalog-warning__inner > span:not(.recovery-alert__outcome) {
			flex: none;
		}

		.recovery-alert__retry {
			align-self: flex-start;
			margin-top: 4px;
		}
	}
</style>
