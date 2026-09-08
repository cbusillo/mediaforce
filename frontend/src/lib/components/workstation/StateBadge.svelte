<script lang="ts">
	type Tone = 'active' | 'ready' | 'wait' | 'fail' | 'idle';

	let {
		tone = 'idle',
		label,
		compact = false,
		quiet = false
	}: {
		tone?: Tone;
		label: string;
		compact?: boolean;
		quiet?: boolean;
	} = $props();
</script>

<span
	class="state-badge state-badge--{tone}"
	class:state-badge--compact={compact}
	class:state-badge--quiet={quiet}
>
	<span class="state-badge__dot"></span>
	{label}
</span>

<style>
	.state-badge {
		--badge-fg: var(--mf-idle-fg);
		--badge-bg: var(--mf-idle-bg);
		--badge-line: var(--mf-idle-line);

		align-items: center;
		background: var(--badge-bg);
		border: 0;
		border-radius: 999px;
		color: var(--badge-fg);
		display: inline-flex;
		font-size: var(--mf-text-xs);
		font-weight: var(--mf-weight-semibold);
		gap: var(--mf-space-3);
		justify-self: start;
		line-height: 1;
		min-height: 24px;
		padding: 0 9px;
		white-space: nowrap;
		width: fit-content;
	}

	.state-badge--compact {
		background: var(--badge-bg);
		min-height: 0;
		padding: 5px 8px;
	}

	.state-badge--quiet {
		background: transparent;
		color: var(--mf-fg-secondary);
	}

	.state-badge--quiet .state-badge__dot {
		background: currentColor;
	}

	.state-badge__dot {
		background: var(--badge-fg);
		border-radius: 50%;
		display: inline-block;
		height: 6px;
		width: 6px;
	}

	.state-badge--active {
		--badge-fg: var(--mf-active-fg);
		--badge-bg: var(--mf-active-bg);
		--badge-line: var(--mf-active-line);
	}

	.state-badge--ready {
		--badge-fg: var(--mf-ready-fg);
		--badge-bg: var(--mf-ready-bg);
		--badge-line: var(--mf-ready-line);
	}

	.state-badge--wait {
		--badge-fg: var(--mf-wait-fg);
		--badge-bg: var(--mf-wait-bg);
		--badge-line: var(--mf-wait-line);
	}

	.state-badge--fail {
		--badge-fg: var(--mf-fail-fg);
		--badge-bg: var(--mf-fail-bg);
		--badge-line: var(--mf-fail-line);
	}
</style>
