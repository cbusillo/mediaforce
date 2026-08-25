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
const DEFAULT_ROUTE_TIMEOUT_MS = 6000;
const SERVER_START_TIMEOUT_MS = 12000;
const NARROW_VIEWPORT = { width: 390, height: 844 };
const APP_ROOT_SELECTOR = ".app-shell";

/**
 * @typedef {object} SmokeFixtureRoute
 * @property {string} label
 * @property {string} route
 * @property {string} marker
 * @property {string=} stageMarker
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
  ["Dashboard scan job", "/api/dashboard/scan-job"],
  ["Dashboard folders", "/api/dashboard/folders"],
  ["Library structure", "/api/dashboard/library"],
  ["Library details", "/api/dashboard/library/details"],
  ["Movie library structure", "/api/dashboard/library/movies"],
  ["Movie library details", "/api/dashboard/library/movies/details"],
  ["Other library structure", "/api/dashboard/library/other"],
  ["Other library details", "/api/dashboard/library/other/details"],
  ["Host status", "/api/hosts?compact=1"],
  ["Settings initial payload", "/api/settings?include_archive_cleanup=0"],
  ["Completed payload", "/api/completed"],
  ["Operator catalog and evidence work", "/api/operator-work"],
];

const routeChecks = [
  ["Library", "/", "Your library"],
  ["Movies Library", "/movies", "MOVIE LIBRARY"],
  ["Other Library", "/other", "Other Library"],
  ["Folders compatibility", "/folders", "Your library"],
  ["Activity", "/ops", "Activity", "Computers"],
  ["Settings", "/settings", "Library and working space", "Work schedule"],
  ["Finished", "/completed", "Finished media"],
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

async function fetchWithTimeout(url, timeoutMs, { expectJson = false } = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  const started = performance.now();
  try {
    const response = await fetch(url, { signal: controller.signal });
    if (!response.ok) {
      throw new Error(`${response.status} ${response.statusText}`);
    }
    if (expectJson) {
      const contentType = response.headers.get("content-type") ?? "";
      if (!contentType.toLowerCase().includes("application/json")) {
        throw new Error(
          `expected JSON, received ${contentType || "no content type"}`,
        );
      }
      await response.json();
    } else {
      await response.arrayBuffer();
    }
    return Math.round(performance.now() - started);
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") {
      throw new Error(`timed out after ${timeoutMs}ms`);
    }
    throw error;
  } finally {
    clearTimeout(timeout);
  }
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
    const elapsedMs = await fetchWithTimeout(`${baseUrl}${route}`, timeoutMs, {
      expectJson: true,
    });
    console.log(`endpoint ok: ${label} ${elapsedMs}ms`);
  }
}

async function checkRoutes(baseUrl, routeChecksForBrowser, timeoutMs) {
  const browser = await chromium.launch({ channel: "chromium" });
  try {
    const page = await browser.newPage({
      viewport: { width: 1440, height: 1000 },
    });
    const pageErrors = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
    for (const [
      label,
      route,
      marker,
      stageMarker = "",
    ] of routeChecksForBrowser) {
      pageErrors.length = 0;
      const started = performance.now();
      const requireFolderReadyMarker = route.startsWith("/folders/");
      await page.goto(`${baseUrl}${route}`, {
        waitUntil: "domcontentloaded",
        timeout: timeoutMs,
      });
      await page.waitForSelector(APP_ROOT_SELECTOR, {
        state: "visible",
        timeout: timeoutMs,
      });
      await page.waitForSelector("main", {
        state: "visible",
        timeout: timeoutMs,
      });
      await page
        .waitForFunction(
          ({ expectedMarker, expectedStageMarker, requireFolderReady }) => {
            if (!document.body.innerText.includes(expectedMarker)) return false;
            if (
              expectedStageMarker &&
              !document.body.innerText.includes(expectedStageMarker)
            )
              return false;
            if (!requireFolderReady) return true;
            const readyMarker = document
              .querySelector("[data-folder-ready-marker]")
              ?.getAttribute("data-folder-ready-marker");
            return Boolean(readyMarker?.includes(expectedMarker));
          },
          {
            expectedMarker: marker,
            expectedStageMarker: stageMarker,
            requireFolderReady: requireFolderReadyMarker,
          },
          { timeout: timeoutMs },
        )
        .catch((error) => {
          throw new Error(
            `${label} did not show marker ${JSON.stringify(marker)} within ${timeoutMs}ms: ${error.message}`,
          );
        });
      const state = await page.evaluate(
        ({ expectedMarker, expectedStageMarker, requireFolderReady }) => {
          const bodyText = document.body.innerText.trim();
          const readyMarker = document
            .querySelector("[data-folder-ready-marker]")
            ?.getAttribute("data-folder-ready-marker");
          return {
            bodyLength: bodyText.length,
            hasMain: document.querySelector("main") !== null,
            hasAppRoot: document.querySelector(".app-shell") !== null,
            hasMarker: bodyText.includes(expectedMarker),
            hasStageMarker:
              !expectedStageMarker || bodyText.includes(expectedStageMarker),
            hasReadyMarker:
              !requireFolderReady ||
              Boolean(readyMarker?.includes(expectedMarker)),
          };
        },
        {
          expectedMarker: marker,
          expectedStageMarker: stageMarker,
          requireFolderReady: requireFolderReadyMarker,
        },
      );
      if (
        !state.hasAppRoot ||
        !state.hasMain ||
        !state.hasMarker ||
        !state.hasStageMarker ||
        !state.hasReadyMarker ||
        state.bodyLength < 80
      ) {
        throw new Error(
          `${label} rendered an incomplete app root: ${JSON.stringify(state)}`,
        );
      }
      if (pageErrors.length > 0) {
        throw new Error(
          `${label} raised browser errors: ${pageErrors.join(" | ")}`,
        );
      }
      if (route === "/ops" && label === "Activity") {
        const bodyText = await page.locator("body").innerText();
        for (const requiredCopy of [
          "Computers",
          "Stop processing",
          "Stop samples",
        ]) {
          if (!bodyText.includes(requiredCopy)) {
            throw new Error(
              `Activity omitted ${JSON.stringify(requiredCopy)}.`,
            );
          }
        }
        const lines = new Set(bodyText.split("\n").map((line) => line.trim()));
        for (const staleCopy of [
          "Processing",
          "Sample checks",
          "Workers",
          "Can make",
          "Reset trust",
          "Prepare password",
        ]) {
          if (lines.has(staleCopy)) {
            throw new Error(
              `Activity exposed stale copy ${JSON.stringify(staleCopy)}.`,
            );
          }
        }
      }
      if (route === "/settings") {
        const bodyText = await page.locator("body").innerText();
        for (const requiredCopy of [
          "Computers",
          "Work schedule",
          "Work runs anytime",
          "Work schedule is off",
        ]) {
          if (!bodyText.includes(requiredCopy)) {
            throw new Error(
              `Settings omitted ${JSON.stringify(requiredCopy)}.`,
            );
          }
        }
        const lines = new Set(bodyText.split("\n").map((line) => line.trim()));
        for (const staleCopy of [
          "Workers",
          "Schedule",
          "Window key",
          "Remove window",
        ]) {
          if (lines.has(staleCopy)) {
            throw new Error(
              `Settings exposed stale copy ${JSON.stringify(staleCopy)}.`,
            );
          }
        }
      }
      const elapsedMs = Math.round(performance.now() - started);
      console.log(`route ok: ${label} ${elapsedMs}ms`);
    }
  } finally {
    await browser.close();
  }
}

async function checkLibraryStructureWithoutDashboard(
  baseUrl,
  expectedMarker,
  timeoutMs,
) {
  const browser = await chromium.launch({ channel: "chromium" });
  try {
    const page = await browser.newPage({
      viewport: { width: 1440, height: 1000 },
    });
    const pageErrors = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
    await page.addInitScript(() => {
      const originalFetch = window.fetch.bind(window);
      window.__mediaforceDashboardBlocked = false;
      window.fetch = (input, init) => {
        const url =
          typeof input === "string"
            ? input
            : input instanceof Request
              ? input.url
              : String(input);
        const requestUrl = new URL(url, window.location.origin);
        if (
          requestUrl.pathname === "/api/dashboard" &&
          requestUrl.searchParams.get("preview_limit") === "0"
        ) {
          window.__mediaforceDashboardBlocked = true;
          return new Promise(() => {});
        }
        return originalFetch(input, init);
      };
    });
    await page.goto(`${baseUrl}/`, {
      waitUntil: "domcontentloaded",
      timeout: timeoutMs,
    });
    await page.waitForFunction(
      (marker) => document.body.innerText.includes(marker),
      expectedMarker,
      { timeout: timeoutMs },
    );
    const state = await page.evaluate(
      (marker) => ({
        hasMarker: document.body.innerText.includes(marker),
        stillOpening: document.body.innerText.includes("Opening your library"),
        dashboardBlocked: Boolean(window.__mediaforceDashboardBlocked),
      }),
      expectedMarker,
    );
    if (!state.hasMarker || state.stillOpening || !state.dashboardBlocked) {
      throw new Error(
        `Library structure waited for dashboard hydration: ${JSON.stringify(state)}`,
      );
    }
    if (pageErrors.length > 0) {
      throw new Error(
        `Library structure fallback raised browser errors: ${pageErrors.join(" | ")}`,
      );
    }
    console.log("route ok: Library structure without dashboard hydration");
  } finally {
    await browser.close();
  }
}

async function checkLifecyclePolicyShowIsolation(baseUrl, timeoutMs) {
  const browser = await chromium.launch({ channel: "chromium" });
  try {
    const page = await browser.newPage({
      viewport: { width: 1440, height: 1000 },
    });
    let markSaveRequested = () => {};
    const saveRequested = new Promise((resolve) => {
      markSaveRequested = resolve;
    });
    await page.route(/\/api\/folders\/.*\/series-lifecycle$/, async (route) => {
      if (route.request().method() !== "POST") {
        await route.continue();
        return;
      }
      markSaveRequested();
      await new Promise((resolve) => setTimeout(resolve, 1000));
      await route.continue();
    });
    await page.goto(`${baseUrl}/`, {
      waitUntil: "domcontentloaded",
      timeout: timeoutMs,
    });
    const showButtons = page.locator(".show-list button");
    await showButtons.nth(1).waitFor({ state: "visible", timeout: timeoutMs });
    const originalShowName = (
      await showButtons.nth(0).locator(".show-copy strong").innerText()
    ).trim();
    const alternateShowName = "Example Show";
    const showButton = (name) =>
      page.locator(".show-list button").filter({ hasText: name }).first();
    const policySelect = page.locator(
      'select[aria-describedby="current-season-policy-help"]',
    );
    await page.waitForFunction(
      () => {
        const select = document.querySelector(
          'select[aria-describedby="current-season-policy-help"]',
        );
        return select instanceof HTMLSelectElement && !select.disabled;
      },
      undefined,
      { timeout: timeoutMs },
    );
    await showButton(alternateShowName).click();
    const alternatePolicy = await policySelect.inputValue();
    if (alternatePolicy !== "auto") {
      throw new Error(
        `Lifecycle isolation fixture expected ${alternateShowName} to use auto, received ${alternatePolicy}`,
      );
    }
    await showButton(originalShowName).click();
    const saveCompleted = page
      .waitForResponse(
        (response) =>
          response.request().method() === "POST" &&
          response.url().includes("/series-lifecycle") &&
          response.ok(),
      )
      .then(
        () => null,
        (error) => error,
      );
    await policySelect.selectOption("on");
    await saveRequested;
    await showButton(alternateShowName).click();
    const selectedValue = await policySelect.inputValue();
    if (selectedValue !== "auto") {
      throw new Error(
        `Current-season policy leaked across shows while saving: ${selectedValue}`,
      );
    }
    const saveError = await saveCompleted;
    if (saveError instanceof Error) throw saveError;
    await showButton(originalShowName).click();
    await page.waitForFunction(
      () => {
        const select = document.querySelector(
          'select[aria-describedby="current-season-policy-help"]',
        );
        return select instanceof HTMLSelectElement && select.value === "on";
      },
      undefined,
      { timeout: timeoutMs },
    );
    console.log("route ok: Current-season policy stays scoped to one show");
  } finally {
    await browser.close();
  }
}

async function checkOlderSeasonConfirmation(baseUrl, timeoutMs) {
  const browser = await chromium.launch({ channel: "chromium" });
  try {
    const page = await browser.newPage({
      viewport: { width: 1440, height: 1000 },
    });
    let queueRequests = 0;
    page.on("request", (request) => {
      if (request.url().includes("/queue-older-seasons")) {
        queueRequests += 1;
      }
    });
    await page.goto(`${baseUrl}/folders/tv/Protected%20Ready`, {
      waitUntil: "domcontentloaded",
      timeout: timeoutMs,
    });
    const action = page.getByRole("button", {
      name: "Compress 1 older season",
    });
    await action.waitFor({ state: "visible", timeout: timeoutMs });
    await action.click();
    const dialog = page.getByRole("alertdialog");
    await dialog.waitFor({ state: "visible", timeout: timeoutMs });
    const dialogText = await dialog.innerText();
    for (const marker of [
      "1 season · 1 safety-cleared episode",
      "Safety-cleared size:",
      "Projected savings: about",
      "Season 2 stays original",
      "current-season policy does not change",
    ]) {
      if (!dialogText.includes(marker)) {
        throw new Error(
          `Older-season confirmation missed ${JSON.stringify(marker)}: ${dialogText}`,
        );
      }
    }
    await dialog.getByRole("button", { name: "Go back" }).click();
    await dialog.waitFor({ state: "hidden", timeout: timeoutMs });
    if (queueRequests !== 0) {
      throw new Error(
        "Canceling the older-season confirmation queued production.",
      );
    }
    console.log(
      "route ok: Older-season confirmation is explicit and cancel-safe",
    );
  } finally {
    await browser.close();
  }
}

async function checkActiveTestProgress(baseUrl, route, timeoutMs) {
  const browser = await chromium.launch({ channel: "chromium" });
  try {
    const page = await browser.newPage({
      viewport: { width: 1440, height: 900 },
    });
    await page.goto(`${baseUrl}${route}`, {
      waitUntil: "domcontentloaded",
      timeout: timeoutMs,
    });
    await page
      .getByRole("heading", { name: /^Creating sample for / })
      .waitFor({ state: "visible", timeout: timeoutMs });
    const activeRoom = page.locator(".active-room");
    const activeText = (await activeRoom.textContent()) ?? "";
    for (const expectedText of [
      "Episode target",
      "Mediaforce found settings near",
      "Building comparison clips",
      "2 of 3 comparison clips built",
      "Computer status",
      "Based on 3 comparable completed samples",
    ]) {
      if (!activeText.includes(expectedText)) {
        throw new Error(`Active sample progress omitted: ${expectedText}`);
      }
    }
    if (
      activeText.includes("Your sample is starting") ||
      activeText.includes("searching for settings") ||
      activeText.includes("Step progress") ||
      activeText.includes("Configured goal") ||
      activeText.includes("Test band") ||
      activeText.includes("Worker health") ||
      activeText.includes("for the whole episode")
    ) {
      throw new Error(
        "Active sample progress retained stale or misleading copy.",
      );
    }
    const progressbar = activeRoom.getByRole("progressbar", {
      name: /Building comparison clips/,
    });
    if (
      (await progressbar.getAttribute("aria-valuenow")) !== "2" ||
      (await progressbar.getAttribute("aria-valuemax")) !== "3"
    ) {
      throw new Error(
        "Active sample stage progress did not expose bounded work telemetry.",
      );
    }
    if (await page.locator(".quality-memory").isVisible()) {
      throw new Error(
        "Quality memory competed with the active sample surface.",
      );
    }
    await page.setViewportSize({ width: 390, height: 844 });
    const narrowState = await page.evaluate(() => ({
      overflow:
        document.documentElement.scrollWidth >
        document.documentElement.clientWidth,
      progressVisible: (() => {
        const progress = document.querySelector(".active-progress");
        return progress instanceof HTMLElement && progress.offsetHeight > 0;
      })(),
    }));
    if (narrowState.overflow || !narrowState.progressVisible) {
      throw new Error(
        `Active test progress failed narrow layout: ${JSON.stringify(narrowState)}`,
      );
    }
    console.log("route ok: Active test progress is truthful and responsive");
  } finally {
    await browser.close();
  }
}

async function checkReviewTransitionDedupe(baseUrl, timeoutMs) {
  const browser = await chromium.launch({ channel: "chromium" });
  try {
    const page = await browser.newPage({
      viewport: { width: 1440, height: 900 },
    });
    const transitionPrefix = "tv/Transition Fixture/Season 1/Episode 01.mkv";
    const transitionFolderPrefix = "tv/Transition Fixture/Season 1";
    const siblingPrefix = `${transitionFolderPrefix}/Episode 02.mkv`;
    await page.route(/\/api\/dashboard(?:\?.*)?$/, async (route) => {
      const response = await route.fetch();
      const payload = await response.json();
      payload.encode_queue.running = [
        ...(payload.encode_queue.running ?? []),
        {
          job_id: "web-smoke-transition-encode",
          prefix: transitionFolderPrefix,
          status: "running",
          host: { label: "Smoke fixture" },
          progress: {
            percent_complete: 1,
            current_item_rel_path: transitionPrefix,
          },
        },
        {
          job_id: "web-smoke-transition-sibling",
          prefix: transitionFolderPrefix,
          status: "running",
          host: { label: "Smoke fixture" },
          progress: {
            percent_complete: 2,
            current_item_rel_path: siblingPrefix,
          },
        },
      ];
      payload.encode_queue.running_count = payload.encode_queue.running.length;
      payload.calibration_queue.sample.pending_review = [
        ...(payload.calibration_queue.sample.pending_review ?? []),
        {
          job_id: "web-smoke-transition-sample",
          prefix: transitionPrefix,
          status: "pending_review",
          host: { label: "Smoke fixture" },
          created_at: "2026-08-21T13:54:52+00:00",
          notes:
            "Use the configured runtime-normalized goal, then make a representative test so the operator can judge the picture and sound.",
        },
      ];
      payload.calibration_queue.sample.pending_review_count =
        payload.calibration_queue.sample.pending_review.length;
      await route.fulfill({ response, json: payload });
    });
    await page.goto(`${baseUrl}/ops`, {
      waitUntil: "domcontentloaded",
      timeout: timeoutMs,
    });
    const transitionRows = page
      .getByRole("row")
      .filter({ hasText: "Episode 01.mkv" });
    await transitionRows.first().waitFor({
      state: "visible",
      timeout: timeoutMs,
    });
    if ((await transitionRows.count()) !== 1) {
      throw new Error(
        "Working now repeated one file across encode and sample-review states.",
      );
    }
    const siblingRows = page
      .getByRole("row")
      .filter({ hasText: "Episode 02.mkv" });
    await siblingRows.first().waitFor({
      state: "visible",
      timeout: timeoutMs,
    });
    if ((await siblingRows.count()) !== 1) {
      throw new Error(
        "Working now hid unrelated active encoding that shared the reviewed item's folder.",
      );
    }
    const transitionText = (await transitionRows.first().innerText()) ?? "";
    const normalizedTransitionText = transitionText.toLowerCase();
    if (
      !normalizedTransitionText.includes("waiting") ||
      !normalizedTransitionText.includes("complete") ||
      !normalizedTransitionText.includes("review unavailable") ||
      normalizedTransitionText.includes("review item") ||
      transitionText.includes("2026-08-21T13:54:52+00:00") ||
      normalizedTransitionText.includes("runtime-normalized goal") ||
      normalizedTransitionText.includes("running")
    ) {
      throw new Error(
        `Working now kept the wrong transition state: ${transitionText}`,
      );
    }
    const desktopOverflow = await page.evaluate(
      () =>
        document.documentElement.scrollWidth >
        document.documentElement.clientWidth,
    );
    if (desktopOverflow) {
      throw new Error(
        "Working now transition state caused desktop page overflow.",
      );
    }
    await page.setViewportSize({ width: 390, height: 844 });
    await transitionRows.first().waitFor({
      state: "visible",
      timeout: timeoutMs,
    });
    await siblingRows.first().waitFor({
      state: "visible",
      timeout: timeoutMs,
    });
    const narrowOverflow = await page.evaluate(
      () =>
        document.documentElement.scrollWidth >
        document.documentElement.clientWidth,
    );
    if (narrowOverflow) {
      throw new Error(
        "Working now transition state caused narrow page overflow.",
      );
    }
    console.log("route ok: Working now deduplicates review transitions");
  } finally {
    await browser.close();
  }
}

async function checkComparisonWorkspace(baseUrl, route, timeoutMs) {
  const browser = await chromium.launch({ channel: "chromium" });
  try {
    const page = await browser.newPage({
      viewport: { width: 1440, height: 900 },
    });
    await page.goto(`${baseUrl}${route}`, {
      waitUntil: "domcontentloaded",
      timeout: timeoutMs,
    });
    const openButton = page.getByRole("button", {
      name: "Compare in full screen",
    });
    await openButton.waitFor({ state: "visible", timeout: timeoutMs });
    await openButton.click();
    const workspace = page.getByRole("dialog", {
      name: "Compare picture and sound",
    });
    await workspace.waitFor({ state: "visible", timeout: timeoutMs });
    await workspace.getByRole("button", { name: "One at a time" }).click();
    await workspace
      .getByRole("group", { name: "Picture shown" })
      .getByRole("button", { name: "Original", exact: true })
      .click();
    await workspace.getByRole("button", { name: "Actual size" }).click();
    const state = await workspace.evaluate((element) => ({
      text: element.textContent ?? "",
      hasVisibleOriginal: Boolean(
        element.querySelector(".show-original .media-pane--original"),
      ),
      hasSoundChoice: Boolean(
        element.querySelector('[role="group"][aria-label="Listen to"]'),
      ),
    }));
    if (!state.hasVisibleOriginal || !state.hasSoundChoice) {
      throw new Error(
        `Comparison workspace did not expose expected controls: ${JSON.stringify(state)}`,
      );
    }
    await workspace
      .getByRole("group", { name: "Picture shown" })
      .getByRole("button", { name: "Sample", exact: true })
      .waitFor({ state: "visible", timeout: timeoutMs });
    for (const expectedText of [
      "Sample",
      "Clip sizes only.",
      "Estimated episode output:",
    ]) {
      if (!state.text.includes(expectedText)) {
        throw new Error(`Comparison workspace omitted: ${expectedText}`);
      }
    }
    if (
      /\b(CRF|codec|bitrate|VMAF|XPSNR|synchroni[sz]ation)\b/i.test(state.text)
    ) {
      throw new Error("Comparison workspace exposed implementation language.");
    }
    await workspace.getByRole("button", { name: "Close comparison" }).click();
    await page.waitForFunction(
      () =>
        document.activeElement?.textContent?.includes("Compare in full screen"),
      undefined,
      { timeout: timeoutMs },
    );
    const revisionPanel = page.locator("#revision-pane");
    if ((await revisionPanel.count()) !== 0) {
      throw new Error(
        "Review revision pane was expanded before the operator requested it.",
      );
    }
    for (const label of [
      "Keep this version",
      "Use less space",
      "Improve picture or sound",
    ]) {
      await page.getByRole("button", { name: label, exact: true }).waitFor({
        state: "visible",
        timeout: timeoutMs,
      });
    }
    if (
      (await page
        .getByRole("button", {
          name: "Try better quality",
          exact: true,
        })
        .count()) !== 0
    ) {
      throw new Error(
        "Review page still exposed the overlapping better-quality action.",
      );
    }
    await page
      .getByRole("button", { name: "Improve picture or sound", exact: true })
      .click();
    await revisionPanel.waitFor({ state: "visible", timeout: timeoutMs });
    const sameSizeChoice = revisionPanel.getByRole("radio", {
      name: /Revise at the same size/,
    });
    const roomierChoice = revisionPanel.getByRole("radio", {
      name: /Allow a larger file/,
    });
    if (
      !(await sameSizeChoice.isChecked()) ||
      !(await roomierChoice.isEnabled())
    ) {
      throw new Error(
        "Review revision strategies did not expose the expected defaults.",
      );
    }
    await revisionPanel
      .getByRole("button", { name: "Picture looks soft", exact: true })
      .click();
    await roomierChoice.check();
    if (
      !((await revisionPanel.textContent()) ?? "").includes(
        "has not been judged yet",
      )
    ) {
      throw new Error(
        "Roomier revision copy did not preserve the unjudged-target boundary.",
      );
    }
    await revisionPanel.getByRole("button", { name: "Never mind" }).click();
    await page.waitForFunction(
      () =>
        document.activeElement?.textContent?.includes(
          "Improve picture or sound",
        ),
      undefined,
      { timeout: timeoutMs },
    );
    const trySmallerButton = page.getByRole("button", {
      name: "Use less space",
      exact: true,
    });
    if (await trySmallerButton.isEnabled()) {
      await trySmallerButton.click();
      const smallerDialog = page.getByRole("dialog", {
        name: "Try a smaller version?",
      });
      const manualPicker = page.locator(".goal-room");
      let smallerOutcome;
      try {
        smallerOutcome = await Promise.any([
          smallerDialog
            .waitFor({ state: "visible", timeout: timeoutMs })
            .then(() => "dialog"),
          manualPicker
            .waitFor({ state: "visible", timeout: timeoutMs })
            .then(() => "picker"),
        ]);
      } catch {
        throw new Error(
          "Use less space produced neither the confirmation dialog nor the size picker.",
        );
      }
      if (smallerOutcome === "dialog") {
        const smallerDialogText = (await smallerDialog.textContent()) ?? "";
        for (const expectedText of [
          "Next target: about",
          "Approach: keep shrinking only while picture and sound remain acceptable",
          "Same resolution and quality checks",
          "Revision concerns will be cleared after the smaller sample starts and are not sent with it",
        ]) {
          if (!smallerDialogText.includes(expectedText)) {
            throw new Error(
              `Try-smaller confirmation omitted: ${expectedText}`,
            );
          }
        }
        await page.keyboard.press("Escape");
        await page.waitForFunction(
          () => document.activeElement?.textContent?.includes("Use less space"),
          undefined,
          { timeout: timeoutMs },
        );
      } else {
        await page.goto(`${baseUrl}${route}`, {
          waitUntil: "domcontentloaded",
          timeout: timeoutMs,
        });
        await page
          .getByRole("button", {
            name: "Improve picture or sound",
            exact: true,
          })
          .waitFor({ state: "visible", timeout: timeoutMs });
      }
    } else {
      console.log(
        "review adjustment skipped: Use less space is disabled because no sample host is available",
      );
    }
    await page.setViewportSize({ width: 390, height: 844 });
    await page
      .getByRole("button", { name: "Improve picture or sound", exact: true })
      .click();
    await revisionPanel.waitFor({ state: "visible", timeout: timeoutMs });
    const narrowState = await page.evaluate(() => ({
      pageOverflow:
        document.documentElement.scrollWidth >
        document.documentElement.clientWidth,
      paneVisible: (() => {
        const pane = document.querySelector("#revision-pane");
        if (!(pane instanceof HTMLElement)) return false;
        const style = window.getComputedStyle(pane);
        return (
          !pane.hidden &&
          style.display !== "none" &&
          style.visibility !== "hidden"
        );
      })(),
    }));
    if (narrowState.pageOverflow || !narrowState.paneVisible) {
      throw new Error(
        `Review revision pane failed narrow layout: ${JSON.stringify(narrowState)}`,
      );
    }
    console.log("route ok: Full-screen comparison workspace");
  } finally {
    await browser.close();
  }
}

async function checkNarrowRoutes(baseUrl, routeChecksForNarrow, timeoutMs) {
  const browser = await chromium.launch({ channel: "chromium" });
  try {
    const page = await browser.newPage({
      viewport: NARROW_VIEWPORT,
      deviceScaleFactor: 2,
      isMobile: true,
    });
    const pageErrors = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
    for (const [
      label,
      route,
      marker,
      stageMarker = "",
    ] of routeChecksForNarrow) {
      pageErrors.length = 0;
      const started = performance.now();
      const requireFolderReadyMarker = route.startsWith("/folders/");
      await page.goto(`${baseUrl}${route}`, {
        waitUntil: "domcontentloaded",
        timeout: timeoutMs,
      });
      await page.waitForSelector(APP_ROOT_SELECTOR, {
        state: "visible",
        timeout: timeoutMs,
      });
      await page.waitForSelector("main", {
        state: "visible",
        timeout: timeoutMs,
      });
      await page
        .waitForFunction(
          ({ expectedMarker, expectedStageMarker, requireFolderReady }) => {
            if (!document.body.innerText.includes(expectedMarker)) return false;
            if (
              expectedStageMarker &&
              !document.body.innerText.includes(expectedStageMarker)
            )
              return false;
            if (!requireFolderReady) return true;
            const readyMarker = document
              .querySelector("[data-folder-ready-marker]")
              ?.getAttribute("data-folder-ready-marker");
            return Boolean(readyMarker?.includes(expectedMarker));
          },
          {
            expectedMarker: marker,
            expectedStageMarker: stageMarker,
            requireFolderReady: requireFolderReadyMarker,
          },
          { timeout: timeoutMs },
        )
        .catch((error) => {
          throw new Error(
            `${label} narrow route did not show marker ${JSON.stringify(marker)} within ${timeoutMs}ms: ${error.message}`,
          );
        });
      const state = await page.evaluate(
        ({ expectedMarker, expectedStageMarker, requireFolderReady }) => {
          const bodyText = document.body.innerText.trim();
          const readyMarker = document
            .querySelector("[data-folder-ready-marker]")
            ?.getAttribute("data-folder-ready-marker");
          const visibleWideTables = Array.from(
            document.querySelectorAll("table"),
          )
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
            hasStageMarker:
              !expectedStageMarker || bodyText.includes(expectedStageMarker),
            hasReadyMarker:
              !requireFolderReady ||
              Boolean(readyMarker?.includes(expectedMarker)),
            pageOverflow:
              document.documentElement.scrollWidth > window.innerWidth + 2,
            scrollWidth: document.documentElement.scrollWidth,
            visibleWideTables,
          };
        },
        {
          expectedMarker: marker,
          expectedStageMarker: stageMarker,
          requireFolderReady: requireFolderReadyMarker,
        },
      );
      if (
        state.bodyLength < 80 ||
        !state.hasMarker ||
        !state.hasStageMarker ||
        !state.hasReadyMarker ||
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
    ["Empty Library", "/", "Point Mediaforce at your TV folder"],
    ["Empty Folders", "/folders", "Point Mediaforce at your TV folder"],
    ["Empty Other Library", "/other", "No Other media is indexed"],
    ["Empty Activity", "/ops", "Nothing is running."],
    ["Empty Finished", "/completed", "No finished media match this search"],
  ];
  await checkRoutes(baseUrl, emptyRouteChecks, timeoutMs);
  if (narrow) {
    await checkNarrowRoutes(baseUrl, emptyRouteChecks, timeoutMs);
  }
}

async function checkCompletedCleanupLanguage(baseUrl, timeoutMs) {
  const browser = await chromium.launch({ channel: "chromium" });
  try {
    const page = await browser.newPage({
      viewport: { width: 1440, height: 1000 },
    });
    let completedCleanupRequests = 0;
    let completedReviewRequests = 0;
    page.on("request", (request) => {
      if (request.method() !== "POST") return;
      if (request.url().includes("/api/completed/backups/clear")) {
        completedCleanupRequests += 1;
      }
      if (request.url().includes("/api/completed/originals/confirm-removed")) {
        completedReviewRequests += 1;
      }
    });
    await page.goto(`${baseUrl}/completed`, {
      waitUntil: "domcontentloaded",
      timeout: timeoutMs,
    });
    await page
      .locator("strong:visible", { hasText: "Backups ready to delete" })
      .first()
      .waitFor();
    await page
      .locator("strong:visible", { hasText: "Backups already gone" })
      .first()
      .waitFor();
    await page.waitForFunction(() =>
      document.body.innerText.includes("Cleanup folder"),
    );
    await page
      .getByText("Select at least one folder with original backups.", {
        exact: true,
      })
      .waitFor();
    await page
      .getByText(
        "Select at least one folder whose original backups are already gone.",
        {
          exact: true,
        },
      )
      .waitFor();

    const readyCleanupCheckbox = page
      .getByLabel(/Select .* to delete its original backups/)
      .first();
    const selectedDeleteTrigger = page.getByRole("button", {
      name: "Delete selected original backups",
    });
    await readyCleanupCheckbox.check();
    await selectedDeleteTrigger.click();
    const completedDeleteDialog = page.getByRole("alertdialog", {
      name: "Confirm original backup deletion",
    });
    const selectedDeleteConfirm = completedDeleteDialog.getByRole("button", {
      name: /Delete [\d,]+ original backups?/,
    });
    await selectedDeleteConfirm.waitFor();
    if (
      !(await selectedDeleteConfirm.evaluate(
        (button) => button === document.activeElement,
      ))
    ) {
      throw new Error("Finished delete confirmation did not receive focus.");
    }
    await completedDeleteDialog
      .getByText("This cannot be undone.", { exact: true })
      .waitFor();
    await completedDeleteDialog
      .getByText("Your finished files are not touched.", { exact: false })
      .waitFor();
    await completedDeleteDialog.getByRole("button", { name: "Cancel" }).click();
    await selectedDeleteTrigger.click();
    await readyCleanupCheckbox.uncheck();
    await completedDeleteDialog.waitFor({ state: "hidden" });
    await readyCleanupCheckbox.check();

    await page
      .getByRole("button", { name: "Delete all original backups" })
      .click();
    await completedDeleteDialog
      .getByText(/including folders hidden by your current filters/)
      .waitFor();
    await completedDeleteDialog
      .getByText("This cannot be undone.", { exact: true })
      .waitFor();
    await page.keyboard.press("Escape");
    await completedDeleteDialog.waitFor({ state: "hidden" });

    await page
      .getByLabel(/Select .* to mark already-gone original backups handled/)
      .first()
      .check();
    await page
      .getByRole("button", { name: "Mark backups already gone as handled" })
      .click();
    const completedReviewDialog = page.getByRole("alertdialog", {
      name: "Confirm already-gone original backups",
    });
    const markHandledConfirm = completedReviewDialog.getByRole("button", {
      name: "Mark handled",
      exact: true,
    });
    await markHandledConfirm.waitFor();
    if (
      !(await markHandledConfirm.evaluate(
        (button) => button === document.activeElement,
      ))
    ) {
      throw new Error(
        "Finished mark-handled confirmation did not receive focus.",
      );
    }
    await completedReviewDialog
      .getByText("Nothing is deleted.", { exact: false })
      .waitFor();
    if (
      await page.getByText("This cannot be undone.", { exact: true }).count()
    ) {
      throw new Error(
        "Mark-handled confirmation incorrectly uses the delete warning.",
      );
    }
    await completedReviewDialog.getByRole("button", { name: "Cancel" }).click();
    if (completedCleanupRequests !== 0 || completedReviewRequests !== 0) {
      throw new Error(
        "Finished cleanup confirmations sent a request before final confirmation.",
      );
    }

    await page.setViewportSize(NARROW_VIEWPORT);
    await page.reload({ waitUntil: "domcontentloaded", timeout: timeoutMs });
    await page
      .locator("strong:visible", { hasText: "Backups ready to delete" })
      .first()
      .waitFor();
    const narrowState = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    }));
    if (narrowState.scrollWidth > narrowState.clientWidth) {
      throw new Error(
        `Finished cleanup overflows horizontally at 390px: ${narrowState.scrollWidth}px > ${narrowState.clientWidth}px`,
      );
    }

    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.goto(`${baseUrl}/settings`, {
      waitUntil: "domcontentloaded",
      timeout: timeoutMs,
    });
    let settingsCleanupDeleteRequests = 0;
    page.on("request", (request) => {
      if (
        request.method() === "POST" &&
        request.url().includes("/api/archive-cleanup/clear")
      ) {
        settingsCleanupDeleteRequests += 1;
      }
    });
    await page.waitForFunction(() =>
      document.body.innerText.includes("Original backups"),
    );
    const settingsDeleteTrigger = page
      .locator(".archive-actions")
      .getByRole("button", {
        name: "Delete all original backups",
        exact: true,
      });
    const settingsConfirmDialog = page.getByRole("alertdialog", {
      name: "Confirm original backup deletion",
    });
    await settingsDeleteTrigger.click();
    const settingsConfirmButton = settingsConfirmDialog.getByRole("button", {
      name: /Delete [\d,]+ original backups?/,
    });
    await settingsConfirmButton.waitFor();
    if (
      !(await settingsConfirmButton.evaluate(
        (button) => button === document.activeElement,
      ))
    ) {
      throw new Error("Settings delete confirmation did not receive focus.");
    }
    await settingsConfirmDialog
      .getByText("This cannot be undone.", { exact: true })
      .waitFor();
    await settingsConfirmDialog
      .getByText("Your finished files are not touched.", { exact: true })
      .waitFor();
    const workingFolderInput = page.getByLabel("Working folder", {
      exact: true,
    });
    const savedWorkingFolder = await workingFolderInput.inputValue();
    await workingFolderInput.fill(`${savedWorkingFolder}-unsaved-smoke`);
    await settingsConfirmDialog.waitFor({ state: "hidden" });
    await page
      .getByText(
        "Save the changed Working folder before deleting original backups from its Cleanup folder.",
        { exact: true },
      )
      .last()
      .waitFor();
    await workingFolderInput.fill(savedWorkingFolder);
    await settingsDeleteTrigger.click();
    await settingsConfirmDialog.waitFor();
    await settingsDeleteTrigger.click();
    await settingsConfirmDialog.waitFor({ state: "hidden" });
    await settingsDeleteTrigger.click();
    await settingsConfirmDialog
      .getByRole("button", { name: "Cancel", exact: true })
      .click();
    if (settingsCleanupDeleteRequests !== 0) {
      throw new Error(
        "Settings cleanup confirmation sent a delete request before final confirmation.",
      );
    }

    const completedResponse = await fetch(`${baseUrl}/api/completed`);
    const completedPayload = await completedResponse.json();
    const missingFolderPayload = {
      ...completedPayload,
      folders_with_backups_count: 0,
      archive_cleanup: {
        ...completedPayload.archive_cleanup,
        archive_root: "",
        file_count: 0,
        total_size_bytes: 0,
        has_cleanup: false,
      },
      folders: completedPayload.folders.map((folder) =>
        folder.archived_backup_count > 0
          ? {
              ...folder,
              cleanup_state: "blocked",
              cleanup_detail:
                "Cleanup folder is not set, so Mediaforce cannot find the original backups.",
            }
          : folder,
      ),
    };
    const missingPage = await browser.newPage({
      viewport: { width: 1440, height: 1000 },
    });
    await missingPage.route("**/api/completed*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(missingFolderPayload),
      }),
    );
    await missingPage.route("**/api/archive-cleanup*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(missingFolderPayload.archive_cleanup),
      }),
    );
    await missingPage.goto(`${baseUrl}/completed`, {
      waitUntil: "domcontentloaded",
      timeout: timeoutMs,
    });
    await missingPage
      .getByText(
        "Cleanup folder is not set, so Mediaforce cannot find the original backups.",
        {
          exact: true,
        },
      )
      .first()
      .waitFor();
    await missingPage
      .getByText(
        "Set a Cleanup folder in Settings before deleting original backups.",
        {
          exact: true,
        },
      )
      .waitFor();
    await missingPage.close();

    console.log("route ok: Finished cleanup language and confirmations");
  } finally {
    await browser.close();
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
        fixtureRoute.stageMarker ?? "",
      ]);
    }
    await checkEndpoints(targetUrl, args.endpointTimeoutMs);
    await checkRoutes(targetUrl, browserRouteChecks, args.routeTimeoutMs);
    if (fixtures?.folderRoutes?.length) {
      await checkCompletedCleanupLanguage(targetUrl, args.routeTimeoutMs);
    }
    if (fixtures?.folderRoutes?.length) {
      const libraryFixture = fixtures.folderRoutes.find((fixtureRoute) =>
        fixtureRoute.route.startsWith("/folders/tv/"),
      );
      if (!libraryFixture) {
        throw new Error("Fixture payload did not include a TV library route.");
      }
      await checkLibraryStructureWithoutDashboard(
        targetUrl,
        libraryFixture.marker,
        args.routeTimeoutMs,
      );
      await checkLifecyclePolicyShowIsolation(targetUrl, args.routeTimeoutMs);
      await checkOlderSeasonConfirmation(targetUrl, args.routeTimeoutMs);
      const samplingFixture = fixtures.folderRoutes.find(
        (fixtureRoute) =>
          fixtureRoute.route === "/folders/tv/Sampling%20Show/Season%201",
      );
      if (!samplingFixture) {
        throw new Error(
          "Fixture payload did not include the active-test route.",
        );
      }
      await checkActiveTestProgress(
        targetUrl,
        samplingFixture.route,
        args.routeTimeoutMs,
      );
      await checkReviewTransitionDedupe(targetUrl, args.routeTimeoutMs);
      const reviewReadyFixture = fixtures.folderRoutes.find(
        (fixtureRoute) =>
          fixtureRoute.route === "/folders/tv/Review%20Ready/Season%201",
      );
      if (!reviewReadyFixture) {
        throw new Error(
          "Fixture payload did not include the review-ready route.",
        );
      }
      await checkComparisonWorkspace(
        targetUrl,
        reviewReadyFixture.route,
        args.routeTimeoutMs,
      );
    }
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
