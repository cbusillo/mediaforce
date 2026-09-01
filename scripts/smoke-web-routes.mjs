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
const { chromium, expect } = require("@playwright/test");

const DEFAULT_ENDPOINT_TIMEOUT_MS = 2000;
const DEFAULT_ROUTE_TIMEOUT_MS = 6000;
const SERVER_START_TIMEOUT_MS = 12000;
const NARROW_VIEWPORT = { width: 390, height: 844 };
const APP_ROOT_SELECTOR = ".app-shell";
const HIGH_SEASON_SERIES_PREFIX = "tv/Long Running Show";
const LIBRARY_METRIC_COPY = [
  "Current size",
  "Estimated output",
  "Estimated space saved",
];

async function launchSmokeBrowser() {
  let browser;
  try {
    browser = await chromium.launch({ channel: "chromium" });
    const page = await browser.newPage();
    await page.close();
    return browser;
  } catch {
    await browser?.close().catch(() => undefined);
    return chromium.launch();
  }
}

const FIXTURE_REQUIRED_COPY = new Map([
  [
    "/folders/tv/Approved%20Show/Season%201",
    [
      "Current size",
      "Estimated output",
      "Estimated space saved",
      "Nothing is queued until you choose the action below.",
      "Compress the season",
    ],
  ],
  [
    "/folders/movies/Target%20Too%20Large",
    [
      "Current size",
      "Estimated output",
      "Estimated space saved",
      "Cannot start",
      "Choose a smaller target in library settings.",
    ],
  ],
  [
    "/folders/movies/Review%20Ready",
    [
      "Ready to review",
      "Estimated output",
      "Sample-backed",
      "Keep this version",
    ],
  ],
  [
    "/folders/movies/Archive%20Ready",
    ["Current file", "Space saved", "Original backup", "Review in Finished"],
  ],
  [
    "/folders/other/Field%20Notes",
    [
      "Files included now",
      "What is included",
      "Files left untouched",
      "Set up sample",
    ],
  ],
]);

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
  ["TV Library", "/", "TV", "", LIBRARY_METRIC_COPY, "TV Library · Mediaforce"],
  [
    "Movie Library",
    "/movies",
    "Movies",
    "",
    LIBRARY_METRIC_COPY,
    "Movie Library · Mediaforce",
  ],
  [
    "Other Library",
    "/other",
    "Other",
    "",
    LIBRARY_METRIC_COPY,
    "Other Library · Mediaforce",
  ],
  [
    "Folders compatibility",
    "/folders",
    "TV Library",
    "",
    LIBRARY_METRIC_COPY,
    "TV Library · Mediaforce",
  ],
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

async function openRoute(page, baseUrl, route, timeoutMs) {
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
}

function routeExpectation(
  route,
  marker,
  stageMarker,
  requiredCopies,
  expectedTitle,
) {
  return {
    expectedMarker: marker,
    expectedStageMarker: stageMarker,
    requiredText: requiredCopies,
    requiredTitle: expectedTitle,
    requireFolderReady: route.startsWith("/folders/"),
  };
}

async function waitForRouteContent(page, expectation, timeoutMs, label) {
  await page
    .waitForFunction(
      ({
        expectedMarker,
        expectedStageMarker,
        requiredText,
        requiredTitle,
        requireFolderReady,
      }) => {
        if (!document.body.innerText.includes(expectedMarker)) return false;
        if (
          expectedStageMarker &&
          !document.body.innerText.includes(expectedStageMarker)
        )
          return false;
        const normalizedBody = document.body.innerText.toLocaleLowerCase();
        if (
          requiredText.some(
            (copy) => !normalizedBody.includes(copy.toLocaleLowerCase()),
          )
        )
          return false;
        if (requiredTitle && document.title !== requiredTitle) return false;
        if (!requireFolderReady) return true;
        const readyMarker = document
          .querySelector("[data-folder-ready-marker]")
          ?.getAttribute("data-folder-ready-marker");
        return Boolean(readyMarker?.includes(expectedMarker));
      },
      expectation,
      { timeout: timeoutMs },
    )
    .catch(async (error) => {
      const state = await readRouteState(page, expectation);
      throw new Error(
        `${label} did not render the required route content within ${timeoutMs}ms: ${JSON.stringify(state)} (${error.message})`,
      );
    });
}

async function readRouteState(page, expectation, inspectNarrowLayout = false) {
  return page.evaluate(
    ({
      expectedMarker,
      expectedStageMarker,
      requiredText,
      requiredTitle,
      requireFolderReady,
      inspectNarrow,
    }) => {
      const bodyText = document.body.innerText.trim();
      const normalizedBody = bodyText.toLocaleLowerCase();
      const readyMarker = document
        .querySelector("[data-folder-ready-marker]")
        ?.getAttribute("data-folder-ready-marker");
      const visibleWideTables = inspectNarrow
        ? Array.from(document.querySelectorAll("table"))
            .map((element) => {
              const rect = element.getBoundingClientRect();
              return {
                text: String(element.textContent ?? "")
                  .trim()
                  .replace(/\s+/g, " ")
                  .slice(0, 80),
                width: Math.round(rect.width),
                height: Math.round(rect.height),
                display: getComputedStyle(element).display,
              };
            })
            .filter(
              (item) => item.width > window.innerWidth + 2 && item.height > 0,
            )
        : [];
      return {
        bodyLength: bodyText.length,
        hasMain: document.querySelector("main") !== null,
        hasAppRoot: document.querySelector(".app-shell") !== null,
        hasMarker: bodyText.includes(expectedMarker),
        hasStageMarker:
          !expectedStageMarker || bodyText.includes(expectedStageMarker),
        missingCopies: requiredText.filter(
          (copy) => !normalizedBody.includes(copy.toLocaleLowerCase()),
        ),
        hasExpectedTitle: !requiredTitle || document.title === requiredTitle,
        hasReadyMarker:
          !requireFolderReady || Boolean(readyMarker?.includes(expectedMarker)),
        pageOverflow:
          inspectNarrow &&
          document.documentElement.scrollWidth > window.innerWidth + 2,
        scrollWidth: document.documentElement.scrollWidth,
        visibleWideTables,
      };
    },
    { ...expectation, inspectNarrow: inspectNarrowLayout },
  );
}

async function checkMovieEstimateCoverage(page, timeoutMs, label) {
  await page.waitForFunction(
    () => {
      const metrics = Array.from(
        document.querySelectorAll(".metric-strip > div"),
      ).map((metric) => ({
        label:
          metric.querySelector(".metric-strip__label")?.textContent?.trim() ??
          "",
        value: metric.querySelector("strong")?.textContent?.trim() ?? "",
        detail: metric.querySelector("small")?.textContent?.trim() ?? "",
      }));
      return ["Estimated output", "Estimated space saved"].every((label) => {
        const metric = metrics.find((candidate) => candidate.label === label);
        return (
          metric != null &&
          /(?:^| · )\d+ of \d+$/.test(metric.detail) &&
          !/At least|At most|Known|No estimate/.test(metric.value)
        );
      });
    },
    undefined,
    { timeout: timeoutMs },
  );
  const reviewReadyRow = page.locator(
    '[data-movie-title-row="movies/Review Ready"]',
  );
  await reviewReadyRow.click();
  if (!(await page.locator(".title-inspector").count())) {
    await reviewReadyRow.locator("xpath=..").locator(".row-inspect").click();
  }
  await page.waitForFunction(
    () =>
      (document.querySelector(".title-inspector")?.textContent ?? "").includes(
        "Estimated from completed samples for every included movie file.",
      ),
    undefined,
    { timeout: timeoutMs },
  );
  const inspectorText = await page.locator(".title-inspector").innerText();
  const normalizedInspectorText = inspectorText.toLocaleLowerCase();
  if (
    !normalizedInspectorText.includes("review ready") ||
    !normalizedInspectorText.includes("ready to review") ||
    normalizedInspectorText.includes("ready to compress") ||
    !normalizedInspectorText.includes("estimated output") ||
    !normalizedInspectorText.includes("estimated space saved") ||
    normalizedInspectorText.includes("no estimate")
  ) {
    throw new Error(
      `${label} did not expose the sampled title estimate: ${JSON.stringify(inspectorText)}`,
    );
  }
}

async function checkCompressionIntentContract(page, timeoutMs, label) {
  const states = [
    [
      "Balance size and detail",
      "Size target",
      "Closest result in the band",
      "Final result must meet the final band.",
    ],
    [
      "Smallest that still looks good",
      "Size ceiling",
      "Low end first",
      "A smaller final result may pass.",
    ],
    [
      "Preserve the reference",
      "Size limit",
      "High fidelity first",
      "Final result must meet the final band.",
    ],
  ];
  for (const [title, sizeLabel, searchLabel, finalHeadline] of states) {
    const option = page.getByRole("radio", { name: title, exact: false });
    const optionText = await option.innerText();
    if (optionText.trim().length <= title.length + 10) {
      throw new Error(`${label} hid the differentiating detail for ${title}.`);
    }
    await option.click();
    await page
      .waitForFunction(
        ({ expectedSize, expectedSearch, expectedFinal }) => {
          const contract =
            document.querySelector(".goal-contract")?.innerText ?? "";
          return (
            contract.includes(expectedSize) &&
            contract.includes(expectedSearch) &&
            contract.includes(expectedFinal)
          );
        },
        {
          expectedSize: sizeLabel,
          expectedSearch: searchLabel,
          expectedFinal: finalHeadline,
        },
        { timeout: timeoutMs },
      )
      .catch(async (error) => {
        const state = await page.evaluate(() => ({
          contract: document.querySelector(".goal-contract")?.innerText ?? "",
          selected: Array.from(document.querySelectorAll('[role="radio"]'))
            .filter(
              (element) => element.getAttribute("aria-checked") === "true",
            )
            .map((element) => String(element.textContent ?? "").trim()),
        }));
        throw new Error(
          `${label} did not render the ${title} contract: ${JSON.stringify(state)} (${error.message})`,
        );
      });
    const announcement =
      (await page.locator('p.sr-only[aria-live="polite"]').textContent()) ?? "";
    if (
      !announcement.includes(title) ||
      !announcement.includes(sizeLabel) ||
      !/\d+ MB/.test(announcement)
    ) {
      throw new Error(
        `${label} did not announce the ${title} contract: ${JSON.stringify(announcement)}`,
      );
    }
  }
  if (
    (await page
      .getByRole("radio", { name: "No visible difference", exact: false })
      .count()) > 0
  ) {
    throw new Error(
      `${label} exposed the behaviorally duplicate transparent option.`,
    );
  }
  const contractText = await page.locator(".goal-contract").innerText();
  for (const requiredCopy of [
    "Sample search band",
    "Final acceptance band",
    "Quality rule",
    "Final acceptance",
  ]) {
    if (!contractText.includes(requiredCopy)) {
      throw new Error(`${label} omitted ${requiredCopy}.`);
    }
  }
  if (contractText.includes("Size is the target.")) {
    throw new Error(`${label} retained the static size-target sentence.`);
  }
}

async function checkActivityQueueFirst(page, label) {
  const activityState = await page.evaluate(() => {
    const main = document.querySelector(".ops__main");
    const queuePanel = main?.querySelector(":scope > .panel");
    const queueHeading = queuePanel?.querySelector(".panel__header h3");
    const queueRect = queuePanel?.getBoundingClientRect();
    const mainChildren = Array.from(main?.children ?? []);
    const queuePanelIndex = queuePanel ? mainChildren.indexOf(queuePanel) : -1;
    const blockerList = document.querySelector(".blocker-list");
    const schedulerConsole = document.querySelector(".scheduler-console");
    const systemDetails = document.querySelector(".system-details");

    return {
      firstPanelTitle: queueHeading?.textContent?.trim() ?? "",
      queueBeginsInViewport:
        Boolean(queueRect) &&
        queueRect.top >= 0 &&
        queueRect.top < window.innerHeight,
      queueStartsNearTop: Boolean(queueRect) && queueRect.top < 180,
      visibleContentBeforeQueue:
        queuePanelIndex >= 0
          ? mainChildren.slice(0, queuePanelIndex).flatMap((element) => {
              const rect = element.getBoundingClientRect();
              const style = window.getComputedStyle(element);
              return style.display !== "none" &&
                style.visibility !== "hidden" &&
                rect.width > 2 &&
                rect.height > 2
                ? [element.tagName.toLowerCase()]
                : [];
            })
          : ["queue-panel-missing"],
      refreshControls: document.querySelectorAll(
        'button[title="Refresh activity now"]',
      ).length,
      blockersStayWithQueue:
        !blockerList || Boolean(queuePanel?.contains(blockerList)),
      controlsStayWithQueue:
        !schedulerConsole || Boolean(queuePanel?.contains(schedulerConsole)),
      systemDetailsCollapsed:
        systemDetails instanceof HTMLDetailsElement
          ? !systemDetails.open
          : false,
    };
  });

  if (
    activityState.firstPanelTitle !== "Working now" ||
    !activityState.queueBeginsInViewport ||
    !activityState.queueStartsNearTop ||
    activityState.visibleContentBeforeQueue.length > 0 ||
    activityState.refreshControls !== 1 ||
    !activityState.blockersStayWithQueue ||
    !activityState.controlsStayWithQueue ||
    !activityState.systemDetailsCollapsed
  ) {
    throw new Error(
      `${label} did not keep the queue-first Activity contract: ${JSON.stringify(activityState)}`,
    );
  }
}

async function checkRoutes(baseUrl, routeChecksForBrowser, timeoutMs) {
  const browser = await launchSmokeBrowser();
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
      requiredCopies = [],
      expectedTitle = "",
    ] of routeChecksForBrowser) {
      pageErrors.length = 0;
      const started = performance.now();
      const expectation = routeExpectation(
        route,
        marker,
        stageMarker,
        requiredCopies,
        expectedTitle,
      );
      await openRoute(page, baseUrl, route, timeoutMs);
      await waitForRouteContent(page, expectation, timeoutMs, label);
      const state = await readRouteState(page, expectation);
      if (
        !state.hasAppRoot ||
        !state.hasMain ||
        !state.hasMarker ||
        !state.hasStageMarker ||
        state.missingCopies.length ||
        !state.hasExpectedTitle ||
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
      if (route === "/movies" && label === "Movie Library") {
        await checkMovieEstimateCoverage(page, timeoutMs, label);
      }
      if (route === "/folders/tv/Example%20Show/Season%201") {
        await checkCompressionIntentContract(page, timeoutMs, label);
      }
      if (route === "/ops" && label === "Activity") {
        const requiredCopies = [
          "Working now",
          "Computers",
          "Stop processing",
          "Stop samples",
        ];
        await page.waitForFunction(
          (required) =>
            required.every((copy) => document.body.innerText.includes(copy)),
          requiredCopies,
          { timeout: timeoutMs },
        );
        const bodyText = await page.locator("body").innerText();
        for (const requiredCopy of requiredCopies) {
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
      if (route === "/ops") {
        await checkActivityQueueFirst(page, label);
      }
      if (route === "/settings") {
        const requiredCopies = [
          "Computers",
          "Work schedule",
          "Work runs anytime",
          "Work schedule is off",
        ];
        await page.waitForFunction(
          (required) =>
            required.every((copy) => document.body.innerText.includes(copy)),
          requiredCopies,
          { timeout: timeoutMs },
        );
        const bodyText = await page.locator("body").innerText();
        for (const requiredCopy of requiredCopies) {
          if (!bodyText.includes(requiredCopy)) {
            throw new Error(
              `Settings omitted ${JSON.stringify(requiredCopy)}.`,
            );
          }
        }
        const lines = new Set(bodyText.split("\n").map((line) => line.trim()));
        for (const staleCopy of ["Workers", "Window key", "Remove window"]) {
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

async function checkLibraryModeLayout(baseUrl, timeoutMs) {
  const browser = await launchSmokeBrowser();
  try {
    for (const viewport of [
      { width: 1440, height: 960 },
      { width: 1024, height: 768 },
      { width: 390, height: 844 },
    ]) {
      const page = await browser.newPage({ viewport });
      const navTops = [];
      for (const route of ["/", "/movies", "/other"]) {
        await openRoute(page, baseUrl, route, timeoutMs);
        await page.locator(".library-layout .library-workbench").waitFor({
          state: "visible",
          timeout: timeoutMs,
        });
        const state = await page.evaluate(() => {
          const heading = document.querySelector(".library-layout > h1");
          const nav = document.querySelector(".library-mode-nav");
          const workTotal = Number(
            document.querySelector(".work-bar__total strong")?.textContent ?? 0,
          );
          const segmentTotal = Array.from(
            document.querySelectorAll(".work-segment"),
          ).reduce(
            (total, segment) =>
              total + Number(segment.getAttribute("data-count") ?? 0),
            0,
          );
          const headingStyle = heading
            ? window.getComputedStyle(heading)
            : null;
          const index = document.querySelector(".library-index");
          const indexStyle = index ? window.getComputedStyle(index) : null;
          const inspectorRect = document
            .querySelector(".library-inspector")
            ?.getBoundingClientRect();
          const selectedRect = document
            .querySelector(
              ".show-row.selected, .title-row.selected, .unit-row.is-selected",
            )
            ?.getBoundingClientRect();
          const nestedScrollCount = Array.from(
            document.querySelectorAll(".library-workbench *"),
          ).filter((element) => {
            const style = window.getComputedStyle(element);
            return (
              ["auto", "scroll"].includes(style.overflowY) &&
              element.scrollHeight > element.clientHeight + 2
            );
          }).length;
          const clippedBadgeCount = Array.from(
            document.querySelectorAll(".library-workbench .state-badge"),
          ).filter((badge) => badge.scrollWidth > badge.clientWidth + 1).length;
          return {
            metricCount: document.querySelectorAll(".metric-strip > div")
              .length,
            navTop: nav?.getBoundingClientRect().top ?? -1,
            visibleHeading:
              headingStyle?.position !== "absolute" ||
              headingStyle?.width !== "1px" ||
              headingStyle?.height !== "1px",
            hasWorkspace: Boolean(document.querySelector(".library-workbench")),
            pageOverflow:
              document.documentElement.scrollWidth >
              document.documentElement.clientWidth,
            indexIsContents: indexStyle?.display === "contents",
            inspectorTop: inspectorRect?.top ?? 0,
            inspectorBottom: inspectorRect?.bottom ?? 0,
            hasInspector: Boolean(inspectorRect),
            pageHeight: document.documentElement.scrollHeight,
            selectedTop: selectedRect?.top ?? 0,
            selectedBottom: selectedRect?.bottom ?? 0,
            inlineGap:
              inspectorRect && selectedRect
                ? Math.round(inspectorRect.top - selectedRect.bottom)
                : null,
            nestedScrollCount,
            clippedBadgeCount,
            workTotal,
            segmentTotal,
          };
        });
        if (
          state.metricCount !== 4 ||
          state.visibleHeading ||
          !state.hasWorkspace ||
          state.pageOverflow ||
          !state.indexIsContents ||
          state.nestedScrollCount !== 0 ||
          state.clippedBadgeCount !== 0 ||
          state.workTotal !== state.segmentTotal ||
          (viewport.width > 760 &&
            (!state.hasInspector ||
              Math.abs(state.inlineGap ?? Number.POSITIVE_INFINITY) > 2 ||
              state.inspectorBottom > state.pageHeight + 2)) ||
          (viewport.width <= 760 && state.hasInspector)
        ) {
          throw new Error(
            `Library mode layout contract failed at ${viewport.width}px ${route}: ${JSON.stringify(state)}`,
          );
        }
        if (route === "/" && viewport.width === 1440) {
          const policyState = await page
            .locator(".show-policy")
            .evaluate((policy) => {
              const policyRect = policy.getBoundingClientRect();
              const selectRect = policy
                .querySelector("select")
                ?.getBoundingClientRect();
              return {
                overflow: policy.scrollWidth > policy.clientWidth + 1,
                selectClipped: selectRect
                  ? selectRect.right > policyRect.right + 1
                  : true,
              };
            });
          if (policyState.overflow || policyState.selectClipped) {
            throw new Error(
              `TV inspector policy clipped at 1440px: ${JSON.stringify(policyState)}`,
            );
          }
        }
        if (route === "/") {
          const highSeasonRow = page.locator(
            `[data-tv-show-row="${HIGH_SEASON_SERIES_PREFIX}"]`,
          );
          await highSeasonRow.click();
          if (viewport.width <= 760) {
            await highSeasonRow
              .locator("xpath=..")
              .getByRole("button", { name: "Inspect" })
              .click();
          }
          const highSeasonInspector = page.locator(".library-inspector");
          await highSeasonInspector.waitFor({
            state: "visible",
            timeout: timeoutMs,
          });
          const highSeasonState = await highSeasonInspector.evaluate(
            (inspector) => {
              const visibleSeasonRows = Array.from(
                inspector.querySelectorAll(".season-row"),
              ).filter(
                (row) => window.getComputedStyle(row).display !== "none",
              );
              const inspectorRect = inspector.getBoundingClientRect();
              const firstSeasonRect =
                visibleSeasonRows[0]?.getBoundingClientRect();
              const arrowInsets = visibleSeasonRows.map((row) => {
                const arrowRect = row
                  .querySelector("svg")
                  ?.getBoundingClientRect();
                return arrowRect
                  ? row.getBoundingClientRect().right - arrowRect.right
                  : Number.POSITIVE_INFINITY;
              });
              const nestedScrollCount = Array.from(
                inspector.querySelectorAll("*"),
              ).filter((element) => {
                const style = window.getComputedStyle(element);
                return (
                  ["auto", "scroll"].includes(style.overflowY) &&
                  element.scrollHeight > element.clientHeight + 2
                );
              }).length;
              return {
                height: inspector.getBoundingClientRect().height,
                seasonInset: firstSeasonRect
                  ? firstSeasonRect.left - inspectorRect.left
                  : Number.POSITIVE_INFINITY,
                seasonWidthRatio: firstSeasonRect
                  ? firstSeasonRect.width / inspectorRect.width
                  : 0,
                maximumArrowInset: Math.max(...arrowInsets),
                nestedScrollCount,
                omittedCopy:
                  inspector
                    .querySelector(".season-list__more")
                    ?.textContent?.trim() ?? "",
                seasonLabels: visibleSeasonRows.map(
                  (row) =>
                    row
                      .querySelector(".season-copy strong")
                      ?.textContent?.trim() ?? "",
                ),
              };
            },
          );
          const expectedSeasonLabels =
            viewport.width <= 760
              ? ["Season 1", "Season 14"]
              : ["Season 1", "Season 2", "Season 13", "Season 14"];
          const expectedOmittedCount = viewport.width <= 760 ? 12 : 10;
          const maximumInspectorHeight = viewport.width <= 760 ? 1000 : 900;
          const allSeasonsLink = highSeasonInspector.getByRole("link", {
            name: "View all 14 seasons in Studio →",
          });
          const allSeasonsHref = await allSeasonsLink.getAttribute("href");
          if (
            JSON.stringify(highSeasonState.seasonLabels) !==
              JSON.stringify(expectedSeasonLabels) ||
            !highSeasonState.omittedCopy.includes(
              `${expectedOmittedCount} more seasons are available.`,
            ) ||
            highSeasonState.height > maximumInspectorHeight ||
            highSeasonState.seasonInset > 32 ||
            highSeasonState.seasonWidthRatio < 0.85 ||
            highSeasonState.maximumArrowInset > 24 ||
            highSeasonState.nestedScrollCount !== 0 ||
            allSeasonsHref !== "/folders/tv/Long%20Running%20Show"
          ) {
            throw new Error(
              `High-season TV detail exceeded its bounded preview contract at ${viewport.width}px: ${JSON.stringify({ ...highSeasonState, allSeasonsHref })}`,
            );
          }
        }
        if (route === "/movies" && viewport.width === 1024) {
          await page.getByPlaceholder("Type part of a title").fill("Review");
          await page.waitForFunction(
            () => document.querySelectorAll(".title-row").length === 1,
            undefined,
            { timeout: timeoutMs },
          );
          const sparseIndexState = await page
            .locator(".title-index")
            .evaluate((index) => {
              const style = window.getComputedStyle(index);
              return {
                display: style.display,
                overflowY: style.overflowY,
              };
            });
          if (
            sparseIndexState.display !== "contents" ||
            sparseIndexState.overflowY !== "visible"
          ) {
            throw new Error(
              `Sparse Movie results retained a bounded index well at 1024px: ${JSON.stringify(sparseIndexState)}`,
            );
          }
        }
        if (viewport.width === 390) {
          await page.waitForLoadState("load", { timeout: timeoutMs });
          await page.evaluate(
            () =>
              new Promise((resolve) => {
                requestAnimationFrame(() => requestAnimationFrame(resolve));
              }),
          );
          const rowSelector =
            route === "/"
              ? ".show-row"
              : route === "/movies"
                ? ".title-row"
                : ".unit-row";
          const rowIdentityAttribute =
            route === "/"
              ? "data-tv-show-row"
              : route === "/movies"
                ? "data-movie-title-row"
                : "data-other-unit-row";
          const candidateRow = page.locator(rowSelector).nth(1);
          const rowIdentity =
            await candidateRow.getAttribute(rowIdentityAttribute);
          if (!rowIdentity) {
            throw new Error(
              `Narrow Library row is missing ${rowIdentityAttribute}: ${route}`,
            );
          }
          const escapedRowIdentity = rowIdentity
            .replaceAll("\\", "\\\\")
            .replaceAll('"', '\\"');
          const selectedRow = page.locator(
            `[${rowIdentityAttribute}="${escapedRowIdentity}"]`,
          );
          await selectedRow.click();
          await expect(selectedRow).toHaveAttribute("aria-pressed", "true", {
            timeout: timeoutMs,
          });
          if (await page.locator(".library-inspector").count()) {
            throw new Error(
              `Narrow Library row opened detail without Inspect: ${route}`,
            );
          }
          const inspectControl = selectedRow
            .locator("xpath=..")
            .locator(".row-inspect");
          await inspectControl.click();
          await page.waitForFunction(
            () => {
              const inspector = document.querySelector(".library-inspector");
              const selected = document.querySelector(
                ".show-row.selected, .title-row.selected, .unit-row.is-selected",
              );
              if (!inspector || !selected) return false;
              return (
                Math.abs(
                  inspector.getBoundingClientRect().top -
                    selected.getBoundingClientRect().bottom,
                ) <= 2
              );
            },
            undefined,
            { timeout: timeoutMs },
          );
          await expect(inspectControl).toHaveAttribute(
            "aria-expanded",
            "true",
            {
              timeout: timeoutMs,
            },
          );
          await expect(inspectControl).toHaveAttribute(
            "aria-label",
            /^Close\s+\S/,
            { timeout: timeoutMs },
          );
          const inspectState = await inspectControl.evaluate((button) => ({
            expanded: button.getAttribute("aria-expanded"),
            label: button.getAttribute("aria-label") ?? "",
          }));
          if (
            inspectState.expanded !== "true" ||
            !/^Close\s+\S/.test(inspectState.label)
          ) {
            throw new Error(
              `Narrow Library Inspect control did not expose its target state: ${route} ${JSON.stringify(inspectState)}`,
            );
          }
        }
        if (viewport.width === 1440) {
          await page.waitForFunction(
            () => {
              const header = document.querySelector(
                ".library-register__header",
              );
              if (!header) return false;
              return (
                document.documentElement.scrollHeight - window.innerHeight >=
                header.getBoundingClientRect().top
              );
            },
            undefined,
            { timeout: timeoutMs },
          );
          await page.evaluate(() => {
            window.scrollTo(
              0,
              Math.min(
                900,
                document.documentElement.scrollHeight - window.innerHeight,
              ),
            );
          });
          await page.waitForFunction(
            () => {
              const header = document.querySelector(
                ".library-register__header",
              );
              return (
                header !== null &&
                Math.abs(header.getBoundingClientRect().top) <= 1
              );
            },
            undefined,
            { timeout: timeoutMs },
          );
          const stickyRegisterState = await page.evaluate(() => {
            const header = document.querySelector(".library-register__header");
            const visibleBadgeStarts = new Set(
              Array.from(
                document.querySelectorAll("[data-library-state] .state-badge"),
              )
                .filter((badge) => {
                  const rect = badge.getBoundingClientRect();
                  return rect.bottom > 0 && rect.top < window.innerHeight;
                })
                .map((badge) => {
                  const badgeRect = badge.getBoundingClientRect();
                  const dotRect = badge
                    .querySelector(".state-badge__dot")
                    ?.getBoundingClientRect();
                  return `${badgeRect.left.toFixed(1)}:${dotRect?.left.toFixed(1) ?? "missing"}`;
                }),
            );
            return {
              headerPosition: header
                ? window.getComputedStyle(header).position
                : "missing",
              headerTop: header?.getBoundingClientRect().top ?? -1,
              scrollY: window.scrollY,
              visibleBadgeStartCount: visibleBadgeStarts.size,
            };
          });
          if (
            stickyRegisterState.headerPosition !== "sticky" ||
            Math.abs(stickyRegisterState.headerTop) > 1 ||
            stickyRegisterState.visibleBadgeStartCount !== 1
          ) {
            throw new Error(
              `Library register scan contract failed at 1440px ${route}: ${JSON.stringify(stickyRegisterState)}`,
            );
          }
          await page.evaluate(() => window.scrollTo(0, 0));
        }
        navTops.push(state.navTop);
      }
      if (Math.max(...navTops) - Math.min(...navTops) > 2) {
        throw new Error(
          `Library mode navigation shifted at ${viewport.width}px: ${JSON.stringify(navTops)}`,
        );
      }
      await page.close();
    }
    console.log("route ok: Shared TV, Movies, and Other library layout");
  } finally {
    await browser.close();
  }
}

async function checkLibraryStateReachability(baseUrl, timeoutMs) {
  const browser = await launchSmokeBrowser();
  try {
    const page = await browser.newPage({
      viewport: { width: 1024, height: 768 },
    });
    for (const route of ["/", "/movies", "/other"]) {
      await openRoute(page, baseUrl, route, timeoutMs);
      await page.locator(".library-layout .library-workbench").waitFor({
        state: "visible",
        timeout: timeoutMs,
      });
      await page.waitForTimeout(250);
      const segments = await page
        .locator("button.work-segment[data-state][data-count]")
        .evaluateAll((elements) =>
          elements.map((element) => ({
            key: element.getAttribute("data-state") ?? "",
            count: Number(element.getAttribute("data-count") ?? 0),
            label: element.textContent?.replace(/\s+/g, " ").trim() ?? "",
          })),
        );
      if (route === "/" && !segments.length) {
        throw new Error("TV Library exposed no reachable current-work state.");
      }
      let foundCannotStart = false;
      for (const segment of segments) {
        if (!segment.key || segment.count <= 0) continue;
        if (segment.label.includes("Cannot start")) foundCannotStart = true;
        await page
          .locator(`button.work-segment[data-state="${segment.key}"]`)
          .click();
        await page.waitForTimeout(100);
        const rowState = await page.evaluate((key) => {
          const rows = Array.from(
            document.querySelectorAll("[data-library-state]"),
          );
          return {
            count: rows.length,
            keys: [
              ...new Set(
                rows.map((row) => row.getAttribute("data-library-state")),
              ),
            ],
            active:
              document
                .querySelector(`button.work-segment[data-state="${key}"]`)
                ?.getAttribute("aria-pressed") === "true",
          };
        }, segment.key);
        if (
          !rowState.active ||
          rowState.count !== segment.count ||
          rowState.keys.length !== 1 ||
          rowState.keys[0] !== segment.key
        ) {
          throw new Error(
            `Library state summary did not match rows for ${segment.key} at ${route}: ${JSON.stringify({ segment, rowState })}`,
          );
        }
      }
      if (route === "/movies" && !foundCannotStart) {
        throw new Error(
          "Movie Library did not expose the Cannot start state through its shared filter.",
        );
      }
    }
    await page.close();
    console.log(
      "route ok: Shared Library state filters reach every summarized row",
    );
  } finally {
    await browser.close();
  }
}

async function checkSeriesSeasonIndex(baseUrl, timeoutMs) {
  const browser = await launchSmokeBrowser();
  try {
    for (const viewport of [
      { width: 1440, height: 960 },
      { width: 390, height: 844 },
    ]) {
      const page = await browser.newPage({ viewport });
      await openRoute(
        page,
        baseUrl,
        "/folders/tv/Long%20Running%20Show",
        timeoutMs,
      );
      const index = page.locator(".series-season-index");
      await index.waitFor({ state: "visible", timeout: timeoutMs });
      const state = await index.evaluate((element) => {
        const rows = Array.from(
          element.querySelectorAll("[data-season-prefix]"),
        );
        return {
          rowCount: rows.length,
          prefixes: rows.map((row) => row.getAttribute("data-season-prefix")),
          hrefs: rows.map((row) => row.getAttribute("href")),
          nestedScrollCount: Array.from(element.querySelectorAll("*")).filter(
            (child) => {
              const style = window.getComputedStyle(child);
              return (
                ["auto", "scroll"].includes(style.overflowY) &&
                child.scrollHeight > child.clientHeight + 2
              );
            },
          ).length,
        };
      });
      const expectedPrefixes = Array.from(
        { length: 14 },
        (_, index) => `tv/Long Running Show/Season ${index + 1}`,
      );
      if (
        state.rowCount !== 14 ||
        JSON.stringify(state.prefixes) !== JSON.stringify(expectedPrefixes) ||
        state.hrefs.some(
          (href, index) =>
            href !== `/folders/tv/Long%20Running%20Show/Season%20${index + 1}`,
        ) ||
        state.nestedScrollCount !== 0
      ) {
        throw new Error(
          `Series season index contract failed at ${viewport.width}px: ${JSON.stringify(state)}`,
        );
      }
      await page.close();
    }
    console.log("route ok: Show-level Studio exposes every season");
  } finally {
    await browser.close();
  }
}

async function checkSeriesSeasonContextFailures(baseUrl, timeoutMs) {
  const browser = await launchSmokeBrowser();
  try {
    const seasonPage = await browser.newPage({
      viewport: { width: 1024, height: 768 },
    });
    const seasonRequests = [];
    seasonPage.on("request", (request) => {
      if (request.url().includes("/api/dashboard/library/details")) {
        seasonRequests.push(request.url());
      }
    });
    await openRoute(
      seasonPage,
      baseUrl,
      "/folders/tv/Example%20Show/Season%201",
      timeoutMs,
    );
    await seasonPage.waitForFunction(
      () => document.body.innerText.includes("Choose a size for Season 1"),
      undefined,
      { timeout: timeoutMs },
    );
    if (seasonRequests.length) {
      throw new Error(
        `Season Studio fetched show-level catalog context: ${JSON.stringify(seasonRequests)}`,
      );
    }
    await seasonPage.close();

    const showPage = await browser.newPage({
      viewport: { width: 1024, height: 768 },
    });
    await showPage.route("**/api/dashboard/library/details", (route) =>
      route.abort(),
    );
    await openRoute(
      showPage,
      baseUrl,
      "/folders/tv/Long%20Running%20Show",
      timeoutMs,
    );
    await showPage.locator(".series-season-index").waitFor({
      state: "visible",
      timeout: timeoutMs,
    });
    const indexText = await showPage
      .locator(".series-season-index")
      .innerText();
    if (
      !indexText.includes("The complete season list is unavailable.") ||
      indexText.includes("No catalog seasons found for this show.")
    ) {
      throw new Error(
        `Show-level Studio hid a season-context failure: ${JSON.stringify(indexText)}`,
      );
    }
    await showPage.close();
    console.log(
      "route ok: TV Studio scopes and reports complete-season context",
    );
  } finally {
    await browser.close();
  }
}

async function checkLibraryStructureWithoutDashboard(
  baseUrl,
  expectedMarker,
  timeoutMs,
) {
  const browser = await launchSmokeBrowser();
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
        stillOpening: document.body.innerText.includes("Loading TV library"),
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
  const browser = await launchSmokeBrowser();
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
    const showButtons = page.locator(".show-row");
    await showButtons.nth(1).waitFor({ state: "visible", timeout: timeoutMs });
    const originalShowName = (
      await showButtons.nth(0).locator(".show-copy strong").innerText()
    ).trim();
    const alternateShowName = "Example Show";
    const showButton = (name) =>
      page.locator(".show-row").filter({ hasText: name }).first();
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
  const browser = await launchSmokeBrowser();
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
      "Estimated space saved: about",
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
  const browser = await launchSmokeBrowser();
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
    const expectedTexts = [
      "Episode target",
      "Mediaforce found settings near",
      "Building comparison clips",
      "2 of 3 comparison clips built",
      "Computer status",
      "Based on 3 comparable completed samples",
    ];
    await page.waitForFunction(
      (expected) => {
        const text = document.querySelector(".active-room")?.textContent ?? "";
        return expected.every((value) => text.includes(value));
      },
      expectedTexts,
      { timeout: Math.max(timeoutMs, 20_000) },
    );
    const activeText = (await activeRoom.textContent()) ?? "";
    for (const expectedText of expectedTexts) {
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
  const browser = await launchSmokeBrowser();
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
  const browser = await launchSmokeBrowser();
  try {
    const page = await browser.newPage({
      viewport: { width: 1440, height: 900 },
    });
    await page.goto(`${baseUrl}${route}`, {
      waitUntil: "domcontentloaded",
      timeout: timeoutMs,
    });
    const inlineWorkspace = page.getByRole("group", {
      name: "Original and sample comparison",
    });
    await inlineWorkspace.waitFor({ state: "visible", timeout: timeoutMs });
    const openButton = inlineWorkspace.getByRole("button", {
      name: "Full screen",
    });
    const inlineText =
      (await page.getByLabel("Review facts").textContent()) ?? "";
    for (const expectedText of [
      "Current size",
      "Estimated output",
      "Estimated space saved",
    ]) {
      if (!inlineText.includes(expectedText)) {
        throw new Error(`Comparison ledger omitted: ${expectedText}`);
      }
    }
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
    for (const expectedText of ["Sample"]) {
      if (!state.text.includes(expectedText)) {
        throw new Error(`Comparison workspace omitted: ${expectedText}`);
      }
    }
    if (
      /\b(CRF|codec|bitrate|VMAF|XPSNR|synchroni[sz]ation)\b/i.test(state.text)
    ) {
      throw new Error("Comparison workspace exposed implementation language.");
    }
    for (const forbiddenAction of [
      "Keep this version",
      "Use less space",
      "Improve picture or sound",
    ]) {
      if (
        (await workspace
          .getByRole("button", { name: forbiddenAction, exact: true })
          .count()) !== 0
      ) {
        throw new Error(
          `Fullscreen exposed workflow action: ${forbiddenAction}`,
        );
      }
    }
    await workspace.getByRole("button", { name: "Exit full screen" }).click();
    await page.waitForFunction(
      () => document.activeElement?.textContent?.includes("Full screen"),
      undefined,
      { timeout: timeoutMs },
    );
    const restoredViewingState = await inlineWorkspace.evaluate((element) => ({
      oneAtATime: element.classList.contains("one-at-a-time"),
      originalVisible: element.classList.contains("show-original"),
      actualSize: element.classList.contains("actual-size"),
    }));
    if (
      !restoredViewingState.oneAtATime ||
      !restoredViewingState.originalVisible ||
      !restoredViewingState.actualSize
    ) {
      throw new Error(
        `Fullscreen exit reset viewing state: ${JSON.stringify(restoredViewingState)}`,
      );
    }
    await inlineWorkspace
      .getByRole("button", { name: "Side by side", exact: true })
      .click();
    await inlineWorkspace
      .getByRole("button", { name: "Fit", exact: true })
      .click();
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
    const reviewJump = page.getByRole("button", {
      name: "Review decision",
      exact: true,
    });
    await reviewJump.waitFor({ state: "visible", timeout: timeoutMs });
    const narrowComparisonState = await inlineWorkspace.evaluate((element) => {
      const panes = Array.from(element.querySelectorAll(".media-pane"));
      return {
        visiblePanes: panes.filter((pane) => {
          const style = window.getComputedStyle(pane);
          return style.display !== "none" && style.visibility !== "hidden";
        }).length,
        pageOverflow:
          document.documentElement.scrollWidth >
          document.documentElement.clientWidth,
      };
    });
    if (
      narrowComparisonState.visiblePanes !== 2 ||
      narrowComparisonState.pageOverflow
    ) {
      throw new Error(
        `Narrow comparison did not keep both panes reachable: ${JSON.stringify(narrowComparisonState)}`,
      );
    }
    await reviewJump.click();
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

async function checkSharedComparisonWorkspace(
  baseUrl,
  route,
  label,
  sectionLabel,
  timeoutMs,
) {
  const browser = await launchSmokeBrowser();
  try {
    const page = await browser.newPage({
      viewport: { width: 1440, height: 900 },
    });
    await page.goto(`${baseUrl}${route}`, {
      waitUntil: "domcontentloaded",
      timeout: timeoutMs,
    });
    const reviewSection = page.getByLabel(sectionLabel);
    const inlineWorkspace = reviewSection.getByRole("group", {
      name: "Original and sample comparison",
    });
    await inlineWorkspace.waitFor({ state: "visible", timeout: timeoutMs });
    const openButton = inlineWorkspace.getByRole("button", {
      name: "Full screen",
      exact: true,
    });
    await openButton.click({ timeout: timeoutMs });
    const workspace = page.getByRole("dialog", { name: /Compare picture/ });
    await workspace.waitFor({ state: "visible", timeout: timeoutMs });
    await workspace.getByRole("button", { name: "Exit full screen" }).click();
    await page.waitForFunction(
      () => document.activeElement?.textContent?.includes("Full screen"),
      undefined,
      { timeout: timeoutMs },
    );
    const bodyOverflow = await page.evaluate(
      () => document.body.style.overflow,
    );
    if (bodyOverflow) {
      throw new Error(`${label} comparison left body scrolling locked`);
    }
    await reviewSection
      .getByRole("button", {
        name: "Download combined comparison",
        exact: true,
      })
      .waitFor({ state: "visible", timeout: timeoutMs });
    console.log(`route ok: ${label} inline comparison workspace`);
  } finally {
    await browser.close();
  }
}

async function checkMovieTitleReviewRecovery(baseUrl, timeoutMs) {
  const browser = await launchSmokeBrowser();
  try {
    const page = await browser.newPage({
      viewport: { width: 1440, height: 900 },
    });
    await page.goto(`${baseUrl}/folders/movies/Review%20Ready/Feature.mkv`, {
      waitUntil: "domcontentloaded",
      timeout: timeoutMs,
    });
    const reviewAction = page.getByRole("link", {
      name: "Review title sample",
      exact: true,
    });
    await reviewAction.waitFor({ state: "visible", timeout: timeoutMs });
    if ((await reviewAction.count()) !== 1) {
      throw new Error(
        "Exact-file recovery did not expose one title-review action.",
      );
    }
    const duplicateActions = [
      "Create sample",
      "Set up sample",
      "Set up another sample",
    ];
    const visibleDuplicateActions = await page.evaluate(
      (labels) =>
        Array.from(document.querySelectorAll("button"))
          .filter((element) => {
            const label = String(element.textContent ?? "").trim();
            const rect = element.getBoundingClientRect();
            return labels.includes(label) && rect.width > 0 && rect.height > 0;
          })
          .map((element) => String(element.textContent ?? "").trim()),
      duplicateActions,
    );
    if (visibleDuplicateActions.length > 0) {
      throw new Error(
        `Exact-file recovery exposed duplicate sample actions: ${visibleDuplicateActions.join(", ")}`,
      );
    }
    await reviewAction.click();
    await page.waitForURL(/\/folders\/movies\/Review%20Ready(?:\?.*)?$/, {
      timeout: timeoutMs,
    });
    await page
      .getByRole("heading", { name: "Review Ready", exact: true })
      .waitFor({ state: "visible", timeout: timeoutMs });

    for (const recovery of [
      {
        route: "/folders/movies/Validation%20Ready/Feature.mkv",
        status: "Ready to check",
      },
      {
        route: "/folders/movies/Replacement%20Ready%20Large/Feature.mkv",
        status: "Ready to replace",
      },
    ]) {
      await page.goto(`${baseUrl}${recovery.route}`, {
        waitUntil: "domcontentloaded",
        timeout: timeoutMs,
      });
      await page
        .getByText(recovery.status, { exact: true })
        .first()
        .waitFor({ state: "visible", timeout: timeoutMs });
      await page
        .getByRole("link", { name: "Open title workspace", exact: true })
        .waitFor({ state: "visible", timeout: timeoutMs });
      if (
        (await page
          .getByRole("link", { name: "Review title sample", exact: true })
          .count()) > 0
      ) {
        throw new Error(
          `${recovery.route} mislabeled non-review title work as sample review.`,
        );
      }
    }

    await page.goto(`${baseUrl}/folders/movies/Blocked%20Cleanup/Feature.mkv`, {
      waitUntil: "domcontentloaded",
      timeout: timeoutMs,
    });
    await page
      .getByText("The checked replacement is installed.", { exact: false })
      .waitFor({ state: "visible", timeout: timeoutMs });
    for (const staleAction of ["Review title sample", "Open title workspace"]) {
      if (
        (await page
          .getByRole("link", { name: staleAction, exact: true })
          .count()) > 0
      ) {
        throw new Error(
          `Completed exact-file route exposed stale action: ${staleAction}`,
        );
      }
    }
    console.log("route ok: Movie exact-file recovery returns to title review");
  } finally {
    await browser.close();
  }
}

async function checkNarrowRoutes(baseUrl, routeChecksForNarrow, timeoutMs) {
  const browser = await launchSmokeBrowser();
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
      requiredCopies = [],
      expectedTitle = "",
    ] of routeChecksForNarrow) {
      pageErrors.length = 0;
      const started = performance.now();
      const expectation = routeExpectation(
        route,
        marker,
        stageMarker,
        requiredCopies,
        expectedTitle,
      );
      await openRoute(page, baseUrl, route, timeoutMs);
      await waitForRouteContent(
        page,
        expectation,
        timeoutMs,
        `${label} narrow`,
      );
      const state = await readRouteState(page, expectation, true);
      if (
        !state.hasAppRoot ||
        !state.hasMain ||
        state.bodyLength < 80 ||
        !state.hasMarker ||
        !state.hasStageMarker ||
        state.missingCopies.length ||
        !state.hasExpectedTitle ||
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
      if (route === "/movies" && label === "Movie Library") {
        await checkMovieEstimateCoverage(page, timeoutMs, `${label} narrow`);
      }
      if (route === "/folders/tv/Example%20Show/Season%201") {
        await checkCompressionIntentContract(
          page,
          timeoutMs,
          `${label} narrow`,
        );
      }
      if (route === "/ops") {
        await checkActivityQueueFirst(page, `${label} narrow`);
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
    [
      "Empty TV Library",
      "/",
      "No TV shows or seasons found.",
      "",
      LIBRARY_METRIC_COPY,
      "TV Library · Mediaforce",
    ],
    [
      "Empty Folders",
      "/folders",
      "No TV shows or seasons found.",
      "",
      LIBRARY_METRIC_COPY,
      "TV Library · Mediaforce",
    ],
    [
      "Empty Movie Library",
      "/movies",
      "No movies found.",
      "",
      LIBRARY_METRIC_COPY,
      "Movie Library · Mediaforce",
    ],
    [
      "Empty Other Library",
      "/other",
      "No Other media found.",
      "",
      LIBRARY_METRIC_COPY,
      "Other Library · Mediaforce",
    ],
    ["Empty Activity", "/ops", "Nothing is running."],
    ["Empty Finished", "/completed", "No finished media match this search"],
  ];
  await checkRoutes(baseUrl, emptyRouteChecks, timeoutMs);
  if (narrow) {
    await checkNarrowRoutes(baseUrl, emptyRouteChecks, timeoutMs);
  }
}

async function checkCompletedCleanupLanguage(baseUrl, timeoutMs) {
  const browser = await launchSmokeBrowser();
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
    const completedRegister = page.locator(".completed-table");
    await completedRegister.waitFor();
    const completedPanel = completedRegister.locator(
      "xpath=ancestor::section[contains(concat(' ', normalize-space(@class), ' '), ' panel ')]",
    );
    if ((await completedPanel.count()) !== 1) {
      throw new Error(
        "Finished media did not render as one coherent register panel.",
      );
    }
    for (const selector of [
      ".completed-filter",
      ".selection-bar",
      ".cleanup-actions",
      ".completed-table",
    ]) {
      if ((await completedPanel.locator(selector).count()) !== 1) {
        throw new Error(`Finished register is missing ${selector}.`);
      }
    }
    await completedPanel
      .getByText("Review resolution", { exact: true })
      .waitFor();
    await completedPanel
      .getByText("Destructive cleanup", { exact: true })
      .waitFor();
    await completedRegister
      .getByText("Backups ready to delete", { exact: true })
      .waitFor();
    await completedRegister
      .getByText("Backups already gone", { exact: true })
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
    const reviewTrigger = page.getByRole("button", {
      name: "Mark backups already gone as handled",
    });
    const globalDeleteTrigger = page.getByRole("button", {
      name: "Delete all original backups",
    });
    for (const trigger of [
      selectedDeleteTrigger,
      reviewTrigger,
      globalDeleteTrigger,
    ]) {
      if ((await trigger.getAttribute("aria-controls")) !== null) {
        throw new Error(
          "A resting Finished action references a confirmation panel that is not rendered.",
        );
      }
    }
    for (const trigger of [selectedDeleteTrigger, reviewTrigger]) {
      const describedBy = await trigger.getAttribute("aria-describedby");
      if (
        !describedBy ||
        !(await page.locator(`#${describedBy}`).isVisible())
      ) {
        throw new Error(
          "A disabled Finished action is not bound to its visible reason.",
        );
      }
    }
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
      !(await completedDeleteDialog.evaluate(
        (dialog) => dialog === document.activeElement,
      ))
    ) {
      throw new Error(
        "Finished delete confirmation panel did not receive focus.",
      );
    }
    if (
      (await selectedDeleteTrigger.getAttribute("aria-controls")) !==
      "selected-cleanup-confirm"
    ) {
      throw new Error(
        "Finished delete trigger did not bind to its rendered confirmation.",
      );
    }
    const confirmRestStyle = await selectedDeleteConfirm.evaluate((button) => {
      const style = getComputedStyle(button);
      return {
        background: style.backgroundColor,
        border: style.borderColor,
        color: style.color,
      };
    });
    await selectedDeleteConfirm.hover();
    const confirmHoverStyle = await selectedDeleteConfirm.evaluate((button) => {
      const style = getComputedStyle(button);
      return {
        background: style.backgroundColor,
        border: style.borderColor,
        color: style.color,
      };
    });
    if (
      JSON.stringify(confirmRestStyle) !== JSON.stringify(confirmHoverStyle)
    ) {
      throw new Error(
        "Finished irreversible confirmation weakened its destructive state on hover.",
      );
    }
    await selectedDeleteTrigger.hover();
    const [armedDeleteStyle, unarmedDeleteStyle, reviewStyle] =
      await Promise.all([
        selectedDeleteTrigger.evaluate((button) => {
          const style = getComputedStyle(button);
          return {
            background: style.backgroundColor,
            border: style.borderColor,
            color: style.color,
          };
        }),
        globalDeleteTrigger.evaluate((button) => {
          const style = getComputedStyle(button);
          return {
            background: style.backgroundColor,
            border: style.borderColor,
            color: style.color,
          };
        }),
        reviewTrigger.evaluate((button) => {
          const style = getComputedStyle(button);
          return {
            background: style.backgroundColor,
            border: style.borderColor,
            color: style.color,
          };
        }),
      ]);
    if (
      JSON.stringify(armedDeleteStyle) === JSON.stringify(unarmedDeleteStyle)
    ) {
      throw new Error(
        "Armed Finished delete action lost its stronger state on hover.",
      );
    }
    if (JSON.stringify(armedDeleteStyle) === JSON.stringify(reviewStyle)) {
      throw new Error(
        "Armed Finished delete action incorrectly uses the review action palette.",
      );
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

    await globalDeleteTrigger.click();
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
    await reviewTrigger.click();
    const completedReviewDialog = page.getByRole("alertdialog", {
      name: "Confirm already-gone original backups",
    });
    const markHandledConfirm = completedReviewDialog.getByRole("button", {
      name: "Mark handled",
      exact: true,
    });
    await markHandledConfirm.waitFor();
    if (
      !(await completedReviewDialog.evaluate(
        (dialog) => dialog === document.activeElement,
      ))
    ) {
      throw new Error(
        "Finished mark-handled confirmation panel did not receive focus.",
      );
    }
    if (
      (await reviewTrigger.getAttribute("aria-controls")) !==
      "review-cleanup-confirm"
    ) {
      throw new Error(
        "Finished review trigger did not bind to its rendered confirmation.",
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

    const completedHistoryPayload = await fetch(
      `${baseUrl}/api/completed`,
    ).then((response) => response.json());
    await page.getByRole("tab", { name: "History" }).click();
    const renderedHistoryRows = page.locator(
      ".history-list--wide .history-row",
    );
    await renderedHistoryRows.first().waitFor();
    if (
      (await renderedHistoryRows.count()) !==
      completedHistoryPayload.history.length
    ) {
      throw new Error(
        "Finished history did not render every event returned by the completed API.",
      );
    }

    await page.setViewportSize(NARROW_VIEWPORT);
    await page.reload({ waitUntil: "domcontentloaded", timeout: timeoutMs });
    await page
      .locator(".completed-table")
      .getByText("Backups ready to delete", { exact: true })
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
    const folderRoutes = fixtures?.folderRoutes ?? [];
    const browserRouteChecks = [...routeChecks];
    for (const fixtureRoute of folderRoutes) {
      browserRouteChecks.push([
        fixtureRoute.label,
        fixtureRoute.route,
        fixtureRoute.marker,
        fixtureRoute.stageMarker ?? "",
        FIXTURE_REQUIRED_COPY.get(fixtureRoute.route) ?? [],
      ]);
    }
    await checkEndpoints(targetUrl, args.endpointTimeoutMs);
    await checkRoutes(targetUrl, browserRouteChecks, args.routeTimeoutMs);
    if (folderRoutes.length) {
      await checkLibraryModeLayout(targetUrl, args.routeTimeoutMs);
      await checkLibraryStateReachability(targetUrl, args.routeTimeoutMs);
      await checkSeriesSeasonIndex(targetUrl, args.routeTimeoutMs);
      await checkSeriesSeasonContextFailures(targetUrl, args.routeTimeoutMs);
      await checkCompletedCleanupLanguage(targetUrl, args.routeTimeoutMs);
    }
    if (folderRoutes.length) {
      const libraryFixture = folderRoutes.find((fixtureRoute) =>
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
      const samplingFixture = folderRoutes.find(
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
      const reviewReadyFixture = folderRoutes.find(
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
      for (const [label, route, sectionLabel] of [
        ["Movie Studio", "/folders/movies/Review%20Ready", "Movie comparison"],
        ["Other Studio", "/folders/other/Review%20Ready", "Folder comparison"],
      ]) {
        await checkSharedComparisonWorkspace(
          targetUrl,
          route,
          label,
          sectionLabel,
          args.routeTimeoutMs,
        );
      }
      await checkMovieTitleReviewRecovery(targetUrl, args.routeTimeoutMs);
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
