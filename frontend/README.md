# Mediaforce Frontend

This is the SvelteKit frontend for Mediaforce.

It owns the user UI for:

- the TV, Movies, and Other libraries and their show, season, and movie pages
- Activity: the queue, computers, and work windows
- Finished: published media and rollback-copy cleanup
- Settings: libraries, hosts, and schedule windows

FastAPI remains the backend/API layer. The frontend talks to it over `/api/*`
and loads review media from `/review-media/*`.

## Local development

See [Local web development](../README.md#local-web-development) for the
platform-specific backend and Vite startup commands and proxy configuration.

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
