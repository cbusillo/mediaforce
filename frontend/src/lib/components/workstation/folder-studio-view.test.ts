import { describe, expect, it } from 'vitest';
import type { HostRuntime } from '$lib/api/types';
import type { FolderCalibrationJob } from '$lib/folders/studio';
import { buildBenchHostOptions, resolveBenchRequestState } from './folder-studio-view';

function host(overrides: Partial<HostRuntime>): HostRuntime {
	return {
		key: 'studio-mini',
		label: 'Studio Mini',
		available: true,
		message: 'ready',
		missing_paths: [],
		issues: [],
		detail: null,
		capabilities: ['folder_ai_tuning'],
		priority: 1,
		max_parallel_encodes: 1,
		active_encode_count: 0,
		schedule_profile_label: 'always',
		schedule_detail: 'open',
		active_flag: 'ready',
		active_reason: 'ready',
		...overrides
	};
}

describe('Folder Studio Bench request mapping', () => {
	it('prefers folder-scoped host options and removes empty keys', () => {
		const options = buildBenchHostOptions(
			[
				{ key: 'sample-host', label: 'Sample host', detail: 'folder match', available: true },
				{ key: 'offline', label: 'Offline host', message: 'missing media', available: false },
				{ key: '   ', label: 'Invalid host', available: true }
			],
			[host({ key: 'fallback', label: 'Fallback' })]
		);

		expect(options).toEqual([
			{ key: 'sample-host', label: 'Sample host', detail: 'folder match', available: true },
			{ key: 'offline', label: 'Offline host', detail: 'missing media', available: false }
		]);
	});

	it('enables send only for a note, available host, and inactive sample job', () => {
		const options = buildBenchHostOptions(undefined, [
			host({}),
			host({ key: 'offline', available: false })
		]);

		expect(resolveBenchRequestState('', 'studio-mini', options, null, false)).toMatchObject({
			disabled: true,
			blocker: 'Describe the Bench request before sending.'
		});
		expect(
			resolveBenchRequestState('try a smaller sample', 'offline', options, null, false)
		).toMatchObject({
			disabled: true,
			blocker: 'Selected host is not available right now.'
		});
		expect(
			resolveBenchRequestState(
				'try a smaller sample',
				'studio-mini',
				options,
				{ status: 'running' } as FolderCalibrationJob,
				false
			)
		).toMatchObject({
			disabled: true,
			blocker: 'A sample job is already active for this folder.',
			activeCalibrationJob: true
		});
		expect(
			resolveBenchRequestState(
				'try a smaller sample',
				'studio-mini',
				options,
				{ status: 'failed' } as FolderCalibrationJob,
				false
			)
		).toMatchObject({ disabled: false, blocker: '', activeCalibrationJob: false });
	});
});
