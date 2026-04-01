import { resolve } from 'node:path';

import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vitest/config';
import { loadEnv } from 'vite';

const DEFAULT_BACKEND_ORIGIN = 'http://127.0.0.1:8777';
const DEFAULT_FRONTEND_HOST = '127.0.0.1';
const DEFAULT_FRONTEND_PORT = 4173;

function preferredEnv(
	env: Record<string, string | undefined>,
	...keys: string[]
): string | undefined {
	for (const key of keys) {
		const value = env[key]?.trim();
		if (value) {
			return value;
		}
	}
	return undefined;
}

function resolveFrontendPort(env: Record<string, string | undefined>): number {
	const rawValue = preferredEnv(env, 'MEDIAFORCE_FRONTEND_DEV_PORT');
	if (!rawValue) {
		return DEFAULT_FRONTEND_PORT;
	}
	const port = Number.parseInt(rawValue, 10);
	return Number.isInteger(port) && port > 0 ? port : DEFAULT_FRONTEND_PORT;
}

function resolveBackendOrigin(env: Record<string, string | undefined>): string {
	const explicitOrigin = preferredEnv(env, 'MEDIAFORCE_FRONTEND_API_ORIGIN');
	if (explicitOrigin) {
		return explicitOrigin.replace(/\/+$/, '');
	}
	const backendPort = preferredEnv(env, 'MEDIAFORCE_WEB_PORT');
	if (!backendPort) {
		return DEFAULT_BACKEND_ORIGIN;
	}
	return `http://127.0.0.1:${backendPort}`;
}

export default defineConfig(({ mode }) => {
	const repoRoot = resolve(import.meta.dirname, '..');
	const env = {
		...loadEnv(mode, repoRoot, ''),
		...loadEnv(mode, import.meta.dirname, ''),
		...process.env
	};
	const frontendHost = preferredEnv(env, 'MEDIAFORCE_FRONTEND_DEV_HOST') ?? DEFAULT_FRONTEND_HOST;
	const frontendPort = resolveFrontendPort(env);
	const backendOrigin = resolveBackendOrigin(env);

	return {
		plugins: [sveltekit()],
		server: {
			host: frontendHost,
			port: frontendPort,
			strictPort: true,
			proxy: {
				'/api': {
					target: backendOrigin,
					changeOrigin: false
				},
				'/review-media': {
					target: backendOrigin,
					changeOrigin: false
				}
			}
		},
		test: {
			expect: { requireAssertions: true },
			projects: [
				{
					extends: './vite.config.ts',
					test: {
						name: 'server',
						environment: 'node',
						include: ['src/**/*.{test,spec}.{js,ts}'],
						exclude: ['src/**/*.svelte.{test,spec}.{js,ts}']
					}
				}
			]
		}
	};
});
