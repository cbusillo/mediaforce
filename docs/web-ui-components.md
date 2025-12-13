# Mediaforce Web UI Components

Conventions for the Mediaforce web UI: where UI code lives, which CSS files to edit,
the shared component class vocabulary, and how to safely use dynamic classes.

## Layout

```text
src/mediaforce/web/
├── templates/              # Jinja2 page templates
└── static/
    ├── css/
    │   ├── tailwind.input.css  # source (edit this)
    │   └── tailwind.css        # generated (do not edit)
    └── js/
        ├── api.js              # fetch helpers (mfApi)
        ├── ui.js               # UI helpers (mfUi)
        ├── common.js           # shared utilities (mfCommon)
        └── pages/              # page-specific modules
```

## Tailwind: what to edit

- Edit `src/mediaforce/web/static/css/tailwind.input.css`.
- Rebuild via `npm run tailwind:dev`.
- Do not hand-edit `src/mediaforce/web/static/css/tailwind.css`.

Tailwind config lives in `tailwind.config.js` (content paths, theme colors, safelist).

## Component classes

Prefer using the shared component classes defined in `tailwind.input.css`
under `@layer components`.

Buttons

```html
<button class="btn">Default</button>
<button class="btn btn-sm">Small</button>
<button class="btn btn-primary">Primary</button>
<button class="btn btn-success">Success</button>
<button class="btn btn-warning">Warning</button>
<button class="btn btn-danger">Danger</button>
<button class="btn btn-ghost">Ghost</button>
```

Cards

```html
<div class="card">Standard content card</div>
<div class="stat-card">KPI/stat card</div>
<div class="mini-card">Compact card</div>
<div class="card-grid">...</div>
```

Badges, pills, and chips

```html
<span class="badge-tier">Tier label</span>
<span class="pill">Default pill</span>
<span class="pill pill-on">Active</span>
<span class="pill pill-off">Inactive</span>
<div class="chip">Inline status</div>
```

Modals

```html
<div class="modal-overlay hidden" id="my-modal">
  <div class="modal-card">...</div>
</div>
```

Forms

```html
<label class="form-label">Label</label>
<input class="form-input" type="text" />
<select class="form-select">...</select>
```

Tables

```html
<table class="table-zebra">
  <thead><tr><th class="sortable">Column</th></tr></thead>
  ...
</table>
```

## Dynamic classes and the Tailwind safelist

Tailwind removes classes it cannot see in scanned source files. If a class is constructed
dynamically (Jinja string interpolation or JS template strings), it must be included
via
`tailwind.config.js`.

Current dynamic patterns:

- `tier-*` (e.g. `tier-pristine`) used in templates and `dashboard.js`
- `text-success|text-warning|text-danger` used by status helpers

Tier classes are defined in `tailwind.input.css`:

- `tier-pristine`
- `tier-good`
- `tier-mediocre`
- `tier-poor`
- `tier-unknown`
- `tier-override`

When adding new dynamic patterns:

1. Define the class in `tailwind.input.css` under `@layer components`.
2. Add a `safelist` pattern in `tailwind.config.js`.
3. Rebuild CSS with `npm run tailwind:dev`.

## JavaScript conventions

Shared namespaces:

- `window.mfApi`: JSON request helpers.
- `window.mfUi`: UI helpers like `setStatus()` and `getPageData()`.
- `window.mfCommon`: shared utilities (e.g., hover preview helper).

Templates can inject page-scoped data using `window.__MF_PAGE__`.
JS reads it via `mfUi.getPageData()`.

## Do / don’t

Do:

- Use component classes (`btn`, `card`, `pill`, etc.) and layer utilities on top.
- Use semantic colors (`text-success`, `bg-surface-card`) instead of raw Tailwind
  palette colors.
- Add a safelist entry whenever you introduce a new dynamic class pattern.

Don’t:

- Edit `tailwind.css` directly.
- Hardcode tier colors in templates; use `tier-*` classes instead.
- Add new UI-only CSS outside Tailwind layers without a strong reason.
