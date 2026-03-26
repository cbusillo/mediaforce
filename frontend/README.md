# Mediaforce Frontend

This is the SvelteKit frontend for the Mediaforce calibration bench.

It owns the operator UI for:

- dashboard and queue status
- folder calibration workstations
- runtime settings for libraries, hosts, and schedule windows

FastAPI remains the backend/API layer. The frontend talks to it over `/api/*`
and loads review media from `/review-media/*`.

## Local development

Start the FastAPI backend first from the project root:

```sh
../scripts/mediaforce-web-dev.sh start
```

Then run the frontend dev server from this directory:

```sh
npm run dev
```

The Vite dev server proxies `/api/*` and `/review-media/*` to the FastAPI
backend on `127.0.0.1:8777`.

## Checks

Type and Svelte diagnostics:

```sh
npm run check
```

Lint and formatting check:

```sh
npm run lint
```

## Production-style build

Build the SPA bundle:

```sh
npm run build
```

The build output is written to `build/`. When that directory exists, FastAPI
serves the built frontend directly so the web UI can run from a single server.
