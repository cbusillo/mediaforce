# Mediaforce Frontend

This is the SvelteKit frontend for Mediaforce.

It owns the operator UI for:

- the TV, Movies, and Other libraries and their show, season, and movie pages
- Activity: the queue, computers, and work windows
- Finished: published media and rollback-copy cleanup
- Settings: libraries, hosts, and schedule windows

FastAPI remains the backend/API layer. The frontend talks to it over `/api/*`
and loads review media from `/review-media/*`.

## Local development

From the project root, start the backend and the Vite dev server together
(Vite listens on `MEDIAFORCE_FRONTEND_DEV_PORT`, default 4173):

```sh
scripts/mediaforce-dev.sh start
```

To run only the backend there and Vite here:

```sh
../scripts/mediaforce-dev.sh start backend
npm run dev
```

The Vite dev server proxies `/api/*` and `/review-media/*` to the FastAPI
backend on `127.0.0.1:8777` by default; set `MEDIAFORCE_FRONTEND_API_ORIGIN`
or `MEDIAFORCE_WEB_PORT` in the repo `.env` to change it.

## Checks

Type and Svelte diagnostics:

```sh
npm run check
```

Lint and formatting check:

```sh
npm run lint
```

Unit tests:

```sh
npm test
```

Managed web route smoke (seeds fixture data and drives every route):

```sh
npm run smoke:web
```

## Production-style build

Build the SPA bundle:

```sh
npm run build
```

The build output is written to `build/`. When that directory exists, FastAPI
serves the built frontend directly so the web UI can run from a single server.
