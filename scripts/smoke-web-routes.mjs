#!/usr/bin/env node

import { execFile as execFileCallback, spawn } from "node:child_process";
import { createRequire } from "node:module";
import net from "node:net";
import { mkdir, stat } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const rootDir = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
);
const require = createRequire(path.join(rootDir, "frontend", "package.json"));
const { chromium } = require("@playwright/test");

const DEFAULT_ENDPOINT_TIMEOUT_MS = 2000;
const DEFAULT_ROUTE_TIMEOUT_MS = 3500;
const SERVER_START_TIMEOUT_MS = 12000;
const NARROW_VIEWPORT = { width: 390, height: 844 };

/**
 * @typedef {object} SmokeFixtureRoute
 * @property {string} label
 * @property {string} route
 * @property {string} marker
 */

/**
 * @typedef {object} SmokeFixtureResult
 * @property {string=} profile
 * @property {number} libraryItems
 * @property {number=} encodeJobs
 * @property {SmokeFixtureRoute[]=} folderRoutes
 */

const endpointChecks = [
  ["Dashboard summary", "/api/dashboard"],
  ["Dashboard folders", "/api/dashboard/folders"],
  ["Host status", "/api/hosts?compact=1"],
  ["Settings initial payload", "/api/settings?include_archive_cleanup=0"],
  ["Completed payload", "/api/completed"],
];

const routeChecks = [
  ["Work", "/", "Work"],
  ["Folders", "/folders", "Folders"],
  ["Ops", "/ops", "Ops"],
  ["Settings", "/settings", "Settings"],
  ["Completed", "/completed", "Completed"],
];

function parseArgs(argv) {
  const parsed = {
    baseUrl: "",
    config: path.join(rootDir, "config", "web-smoke.toml"),
    endpointTimeoutMs: DEFAULT_ENDPOINT_TIMEOUT_MS,
    routeTimeoutMs: DEFAULT_ROUTE_TIMEOUT_MS,
    seedFixtures: null,
    narrow: true,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--base-url") {
      parsed.baseUrl = argv[++index] ?? "";
    } else if (arg === "--config") {
      parsed.config = path.resolve(argv[++index] ?? parsed.config);
    } else if (arg === "--endpoint-timeout-ms") {
      parsed.endpointTimeoutMs = Number(
        argv[++index] ?? parsed.endpointTimeoutMs,
      );
    } else if (arg === "--route-timeout-ms") {
      parsed.routeTimeoutMs = Number(argv[++index] ?? parsed.routeTimeoutMs);
    } else if (arg === "--seed-fixtures") {
      parsed.seedFixtures = true;
    } else if (arg === "--skip-fixture-seed") {
      parsed.seedFixtures = false;
    } else if (arg === "--skip-narrow") {
      parsed.narrow = false;
    } else {
      throw new Error(`Unknown argument: ${arg}`);
    }
  }
  return parsed;
}

function normalizeBaseUrl(value) {
  return value.replace(/\/+$/, "");
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
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      server.close(() => {
        if (address && typeof address === "object") {
          resolve(address.port);
        } else {
          reject(new Error("Could not allocate a local port."));
        }
      });
    });
  });
}

async function prepareSmokeState() {
  const paths = [
    ["scratch", "web-smoke", "source", "movies"],
    ["scratch", "web-smoke", "source", "tv"],
    ["scratch", "web-smoke", "transcode", "_replaced"],
    ["state", "web-smoke", "runs"],
    ["state", "web-smoke", "web"],
    ["state", "web-smoke", "review"],
  ];
  await Promise.all(
    paths.map((parts) =>
      mkdir(path.join(rootDir, ...parts), { recursive: true }),
    ),
  );
}

function execFile(command, args, options) {
  return new Promise((resolve, reject) => {
    execFileCallback(command, args, options, (error, stdout, stderr) => {
      if (error) {
        error.stdout = stdout;
        error.stderr = stderr;
        reject(error);
        return;
      }
      resolve({ stdout, stderr });
    });
  });
}

async function seedSmokeFixtures(configPath, profile = "default") {
  const { stdout, stderr } = await execFile(
    "uv",
    [
      "run",
      "python",
      "scripts/seed-web-smoke-fixtures.py",
      "--config",
      configPath,
      "--profile",
      profile,
    ],
    {
      cwd: rootDir,
      timeout: 15000,
      maxBuffer: 1024 * 1024,
    },
  );
  if (stderr.trim()) {
    console.error(stderr.trim());
  }
  /** @type {SmokeFixtureResult} */
  const result = JSON.parse(stdout.trim());
  console.log(
    `fixture ok: ${result.profile ?? profile} ${result.libraryItems} items seeded, ${result.encodeJobs ?? 0} encode jobs seeded`,
  );
  return result;
}

async function fetchWithTimeout(url, timeoutMs) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  const started = performance.now();
  let response;
  try {
    response = await fetch(url, { signal: controller.signal });
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") {
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
      throw new Error(
        `mediaforce-web exited before serving routes with code ${child.exitCode}`,
      );
    }
    try {
      await fetchWithTimeout(`${baseUrl}/`, 1000);
      return;
    } catch (error) {
      lastError = error;
      await new Promise((resolve) => setTimeout(resolve, 200));
    }
  }
  throw new Error(
    `mediaforce-web did not start within ${SERVER_START_TIMEOUT_MS}ms: ${lastError}`,
  );
}

async function startServer(configPath) {
  const indexPath = path.join(rootDir, "frontend", "build", "index.html");
  if (!(await pathExists(indexPath))) {
    throw new Error(
      "frontend/build/index.html is missing. Run npm --prefix frontend run build first.",
    );
  }
  await prepareSmokeState();
  const port = await freePort();
  const child = spawn(
    "uv",
    [
      "run",
      "mediaforce-web",
      "--config",
      configPath,
      "--host",
      "127.0.0.1",
      "--port",
      String(port),
      "--no-reload",
    ],
    {
      cwd: rootDir,
      env: {
        ...process.env,
        MEDIAFORCE_WEB_RELOAD: "0",
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  const logs = [];
  for (const stream of [child.stdout, child.stderr]) {
    stream.setEncoding("utf8");
    stream.on("data", (chunk) => {
      logs.push(chunk);
      if (logs.join("").length > 8000) logs.splice(0, logs.length - 20);
    });
  }
  const baseUrl = `http://127.0.0.1:${port}`;
  await waitForServer(baseUrl, child);
  return {
    baseUrl,
    stop: async () => {
      if (child.exitCode !== null) return;
      child.kill("SIGTERM");
      let exited = false;
      await new Promise((resolve) => {
        const timeout = setTimeout(resolve, 3000);
        child.once("exit", () => {
          exited = true;
          clearTimeout(timeout);
          resolve();
        });
      });
      if (!exited) child.kill("SIGKILL");
    },
    logs: () => logs.join(""),
  };
}

async function checkEndpoints(baseUrl, timeoutMs) {
  for (const [label, route] of endpointChecks) {
    const elapsedMs = await fetchWithTimeout(`${baseUrl}${route}`, timeoutMs);
    console.log(`endpoint ok: ${label} ${elapsedMs}ms`);
  }
}

async function checkRoutes(baseUrl, routeChecksForBrowser, timeoutMs) {
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage({
      viewport: { width: 1440, height: 1000 },
    });
    const pageErrors = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
    for (const [label, route, marker] of routeChecksForBrowser) {
      pageErrors.length = 0;
      const started = performance.now();
      await page.goto(`${baseUrl}${route}`, {
        waitUntil: "domcontentloaded",
        timeout: timeoutMs,
      });
      await page.waitForSelector(".operator-shell", {
        state: "visible",
        timeout: timeoutMs,
      });
      await page.waitForSelector("main", {
        state: "visible",
        timeout: timeoutMs,
      });
      const state = await page.evaluate((expectedMarker) => {
        const bodyText = document.body.innerText.trim();
        return {
          bodyLength: bodyText.length,
          hasMain: document.querySelector("main") !== null,
          hasShell: document.querySelector(".operator-shell") !== null,
          hasMarker: bodyText.includes(expectedMarker),
        };
      }, marker);
      if (
        !state.hasShell ||
        !state.hasMain ||
        !state.hasMarker ||
        state.bodyLength < 80
      ) {
        throw new Error(
          `${label} rendered an incomplete app shell: ${JSON.stringify(state)}`,
        );
      }
      if (pageErrors.length > 0) {
        throw new Error(
          `${label} raised browser errors: ${pageErrors.join(" | ")}`,
        );
      }
      const elapsedMs = Math.round(performance.now() - started);
      console.log(`route ok: ${label} ${elapsedMs}ms`);
    }
  } finally {
    await browser.close();
  }
}

async function checkNarrowRoutes(baseUrl, routeChecksForNarrow, timeoutMs) {
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage({
      viewport: NARROW_VIEWPORT,
      deviceScaleFactor: 2,
      isMobile: true,
    });
    const pageErrors = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
    for (const [label, route, marker] of routeChecksForNarrow) {
      pageErrors.length = 0;
      const started = performance.now();
      await page.goto(`${baseUrl}${route}`, {
        waitUntil: "domcontentloaded",
        timeout: timeoutMs,
      });
      await page.waitForSelector(".operator-shell", {
        state: "visible",
        timeout: timeoutMs,
      });
      await page.waitForSelector("main", {
        state: "visible",
        timeout: timeoutMs,
      });
      const state = await page.evaluate((expectedMarker) => {
        const bodyText = document.body.innerText.trim();
        const visibleWideTables = Array.from(document.querySelectorAll("table"))
          .map((el) => {
            const rect = el.getBoundingClientRect();
            return {
              text: String(el.textContent ?? "")
                .trim()
                .replace(/\s+/g, " ")
                .slice(0, 80),
              width: Math.round(rect.width),
              height: Math.round(rect.height),
              display: getComputedStyle(el).display,
            };
          })
          .filter(
            (item) => item.width > window.innerWidth + 2 && item.height > 0,
          );
        return {
          bodyLength: bodyText.length,
          hasMarker: bodyText.includes(expectedMarker),
          pageOverflow:
            document.documentElement.scrollWidth > window.innerWidth + 2,
          scrollWidth: document.documentElement.scrollWidth,
          visibleWideTables,
        };
      }, marker);
      if (
        state.bodyLength < 80 ||
        !state.hasMarker ||
        state.pageOverflow ||
        state.visibleWideTables.length
      ) {
        throw new Error(
          `${label} narrow layout failed: ${JSON.stringify(state)}`,
        );
      }
      if (pageErrors.length > 0) {
        throw new Error(
          `${label} raised browser errors in narrow layout: ${pageErrors.join(" | ")}`,
        );
      }
      const elapsedMs = Math.round(performance.now() - started);
      console.log(`narrow route ok: ${label} ${elapsedMs}ms`);
    }
  } finally {
    await browser.close();
  }
}

async function checkEmptyFixtureRoutes(baseUrl, configPath, timeoutMs, narrow) {
  await seedSmokeFixtures(configPath, "empty");
  const emptyRouteChecks = [
    ["Empty Work", "/", "No folders match the current filters"],
    ["Empty Folders", "/folders", "No folders match the current filters"],
    ["Empty Ops", "/ops", "Processing is idle and ready"],
    [
      "Empty Completed",
      "/completed",
      "No completed folders match the active filters",
    ],
  ];
  await checkRoutes(baseUrl, emptyRouteChecks, timeoutMs);
  if (narrow) {
    await checkNarrowRoutes(baseUrl, emptyRouteChecks, timeoutMs);
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  let managedServer = null;
  let targetUrl = args.baseUrl ? normalizeBaseUrl(args.baseUrl) : null;
  let fixtures = null;
  const shouldSeedFixtures = args.seedFixtures ?? !targetUrl;
  try {
    if (shouldSeedFixtures) {
      await prepareSmokeState();
      fixtures = await seedSmokeFixtures(args.config);
    }
    if (!targetUrl) {
      managedServer = await startServer(args.config);
      targetUrl = managedServer.baseUrl;
    }
    const browserRouteChecks = [...routeChecks];
    for (const fixtureRoute of fixtures?.folderRoutes ?? []) {
      browserRouteChecks.push([
        fixtureRoute.label,
        fixtureRoute.route,
        fixtureRoute.marker,
      ]);
    }
    await checkEndpoints(targetUrl, args.endpointTimeoutMs);
    await checkRoutes(targetUrl, browserRouteChecks, args.routeTimeoutMs);
    if (args.narrow) {
      await checkNarrowRoutes(
        targetUrl,
        browserRouteChecks,
        args.routeTimeoutMs,
      );
    }
    if (managedServer && shouldSeedFixtures) {
      await checkEmptyFixtureRoutes(
        targetUrl,
        args.config,
        args.routeTimeoutMs,
        args.narrow,
      );
    }
    console.log(`web route smoke passed: ${targetUrl}`);
  } catch (error) {
    if (managedServer) {
      const logs = managedServer.logs();
      if (logs.trim()) {
        console.error("\nmediaforce-web output:\n");
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
