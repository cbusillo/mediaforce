#!/usr/bin/env node

import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import net from 'node:net';
import { mkdir, stat } from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

const rootDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(path.join(rootDir, 'frontend', 'package.json'));
const { chromium } = require('@playwright/test');

const DEFAULT_ENDPOINT_TIMEOUT_MS = 2000;
const DEFAULT_ROUTE_TIMEOUT_MS = 3500;
const SERVER_START_TIMEOUT_MS = 12000;

const endpointChecks = [
	['Dashboard summary', '/api/dashboard'],
	['Dashboard folders', '/api/dashboard/folders'],
	['Host status', '/api/hosts?compact=1'],
	['Settings initial payload', '/api/settings?include_archive_cleanup=0'],
	['Completed payload', '/api/completed']
];

const routeChecks = [
	['Queue', '/', 'Queue'],
	['Folders', '/folders', 'Queue'],
	['Ops', '/ops', 'Ops'],
	['Settings', '/settings', 'Settings'],
	['Completed', '/completed', 'Completed']
];

function parseArgs(argv) {
	const parsed = {
		baseUrl: '',
		config: path.join(rootDir, 'config', 'web-smoke.toml'),
		endpointTimeoutMs: DEFAULT_ENDPOINT_TIMEOUT_MS,
		routeTimeoutMs: DEFAULT_ROUTE_TIMEOUT_MS
	};
	for (let index = 0; index < argv.length; index += 1) {
		const arg = argv[index];
		if (arg === '--base-url') {
			parsed.baseUrl = argv[++index] ?? '';
		} else if (arg === '--config') {
			parsed.config = path.resolve(argv[++index] ?? parsed.config);
		} else if (arg === '--endpoint-timeout-ms') {
			parsed.endpointTimeoutMs = Number(argv[++index] ?? parsed.endpointTimeoutMs);
		} else if (arg === '--route-timeout-ms') {
			parsed.routeTimeoutMs = Number(argv[++index] ?? parsed.routeTimeoutMs);
		} else {
			throw new Error(`Unknown argument: ${arg}`);
		}
	}
	return parsed;
}

function normalizeBaseUrl(value) {
	return value.replace(/\/+$/, '');
}

async function pathExists(filePath) {
	try {
		await stat(filePath);
		return true;
	} catch {
		return false;
	}
}

async function freePort() {
	return new Promise((resolve, reject) => {
		const server = net.createServer();
		server.once('error', reject);
		server.listen(0, '127.0.0.1', () => {
			const address = server.address();
			server.close(() => {
				if (address && typeof address === 'object') {
					resolve(address.port);
				} else {
					reject(new Error('Could not allocate a local port.'));
				}
			});
		});
	});
}

async function prepareSmokeState() {
	const paths = [
		['scratch', 'web-smoke', 'source', 'movies'],
		['scratch', 'web-smoke', 'source', 'tv'],
		['scratch', 'web-smoke', 'transcode', '_replaced'],
		['state', 'web-smoke', 'runs'],
		['state', 'web-smoke', 'web'],
		['state', 'web-smoke', 'review']
	];
	await Promise.all(paths.map((parts) => mkdir(path.join(rootDir, ...parts), { recursive: true })));
}

async function fetchWithTimeout(url, timeoutMs) {
	const controller = new AbortController();
	const timeout = setTimeout(() => controller.abort(), timeoutMs);
	const started = performance.now();
	let response;
	try {
		response = await fetch(url, { signal: controller.signal });
	} catch (error) {
		if (error instanceof Error && error.name === 'AbortError') {
			throw new Error(`timed out after ${timeoutMs}ms`);
		}
		throw error;
	} finally {
		clearTimeout(timeout);
	}
	const elapsedMs = Math.round(performance.now() - started);
	if (!response.ok) {
		throw new Error(`${response.status} ${response.statusText}`);
	}
	await response.arrayBuffer();
	return elapsedMs;
}

async function waitForServer(baseUrl, child) {
	const deadline = Date.now() + SERVER_START_TIMEOUT_MS;
	let lastError = null;
	while (Date.now() < deadline) {
		if (child.exitCode !== null) {
			throw new Error(`mediaforce-web exited before serving routes with code ${child.exitCode}`);
		}
		try {
			await fetchWithTimeout(`${baseUrl}/`, 1000);
			return;
		} catch (error) {
			lastError = error;
			await new Promise((resolve) => setTimeout(resolve, 200));
		}
	}
	throw new Error(`mediaforce-web did not start within ${SERVER_START_TIMEOUT_MS}ms: ${lastError}`);
}

async function startServer(configPath) {
	const indexPath = path.join(rootDir, 'frontend', 'build', 'index.html');
	if (!(await pathExists(indexPath))) {
		throw new Error('frontend/build/index.html is missing. Run npm --prefix frontend run build first.');
	}
	await prepareSmokeState();
	const port = await freePort();
	const child = spawn(
		'uv',
		[
			'run',
			'mediaforce-web',
			'--config',
			configPath,
			'--host',
			'127.0.0.1',
			'--port',
			String(port),
			'--no-reload'
		],
		{
			cwd: rootDir,
			env: {
				...process.env,
				MEDIAFORCE_WEB_RELOAD: '0'
			},
			stdio: ['ignore', 'pipe', 'pipe']
		}
	);
	const logs = [];
	for (const stream of [child.stdout, child.stderr]) {
		stream.setEncoding('utf8');
		stream.on('data', (chunk) => {
			logs.push(chunk);
			if (logs.join('').length > 8000) logs.splice(0, logs.length - 20);
		});
	}
	const baseUrl = `http://127.0.0.1:${port}`;
	await waitForServer(baseUrl, child);
	return {
		baseUrl,
			stop: async () => {
				if (child.exitCode !== null) return;
				child.kill('SIGTERM');
				let exited = false;
				await new Promise((resolve) => {
					const timeout = setTimeout(resolve, 3000);
					child.once('exit', () => {
						exited = true;
						clearTimeout(timeout);
						resolve();
					});
				});
				if (!exited) child.kill('SIGKILL');
			},
		logs: () => logs.join('')
	};
}

async function checkEndpoints(baseUrl, timeoutMs) {
	for (const [label, route] of endpointChecks) {
		const elapsedMs = await fetchWithTimeout(`${baseUrl}${route}`, timeoutMs);
		console.log(`endpoint ok: ${label} ${elapsedMs}ms`);
	}
}

async function checkRoutes(baseUrl, timeoutMs) {
	const browser = await chromium.launch();
	try {
		const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
		const pageErrors = [];
		page.on('pageerror', (error) => pageErrors.push(error.message));
		for (const [label, route, marker] of routeChecks) {
			pageErrors.length = 0;
			const started = performance.now();
			await page.goto(`${baseUrl}${route}`, { waitUntil: 'domcontentloaded', timeout: timeoutMs });
			await page.waitForSelector('.operator-shell', { state: 'visible', timeout: timeoutMs });
			await page.waitForSelector('main', { state: 'visible', timeout: timeoutMs });
			const state = await page.evaluate((expectedMarker) => {
				const bodyText = document.body.innerText.trim();
				return {
					bodyLength: bodyText.length,
					hasMain: document.querySelector('main') !== null,
					hasShell: document.querySelector('.operator-shell') !== null,
					hasMarker: bodyText.includes(expectedMarker)
				};
			}, marker);
			if (!state.hasShell || !state.hasMain || !state.hasMarker || state.bodyLength < 80) {
				throw new Error(`${label} rendered an incomplete app shell: ${JSON.stringify(state)}`);
			}
			if (pageErrors.length > 0) {
				throw new Error(`${label} raised browser errors: ${pageErrors.join(' | ')}`);
			}
			const elapsedMs = Math.round(performance.now() - started);
			console.log(`route ok: ${label} ${elapsedMs}ms`);
		}
	} finally {
		await browser.close();
	}
}

async function main() {
	const args = parseArgs(process.argv.slice(2));
	let managedServer = null;
	let targetUrl = args.baseUrl ? normalizeBaseUrl(args.baseUrl) : null;
	try {
		if (!targetUrl) {
			managedServer = await startServer(args.config);
			targetUrl = managedServer.baseUrl;
		}
		await checkEndpoints(targetUrl, args.endpointTimeoutMs);
		await checkRoutes(targetUrl, args.routeTimeoutMs);
		console.log(`web route smoke passed: ${targetUrl}`);
	} catch (error) {
		if (managedServer) {
			const logs = managedServer.logs();
			if (logs.trim()) {
				console.error('\nmediaforce-web output:\n');
				console.error(logs.trim());
			}
		}
		console.error(error instanceof Error ? error.message : String(error));
		process.exitCode = 1;
	} finally {
		if (managedServer) await managedServer.stop();
	}
}

await main();
