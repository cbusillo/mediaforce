import { describe, expect, it } from 'vitest';
import { ApiError, fetchJson } from './client';

describe('API client errors', () => {
	it('preserves structured blocker actions from failed responses', async () => {
		const fetcher = async () =>
			new Response(
				JSON.stringify({
					message: 'Motion-pattern evidence is required.',
					next_route: '/ops',
					next_action_label: 'Open Activity'
				}),
				{ status: 409, headers: { 'Content-Type': 'application/json' } }
			);

		try {
			await fetchJson('/api/example', fetcher as typeof fetch);
			expect.unreachable('Expected fetchJson to reject');
		} catch (error) {
			expect(error).toBeInstanceOf(ApiError);
			const apiError = error as ApiError;
			expect(apiError.message).toBe('Motion-pattern evidence is required.');
			expect(apiError.status).toBe(409);
			expect(apiError.payload).toMatchObject({
				next_route: '/ops',
				next_action_label: 'Open Activity'
			});
		}
	});
});
