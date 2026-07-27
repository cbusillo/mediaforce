# Package Builds

`uv build` creates the Mediaforce wheel and source distribution. The wheel
build regenerates `frontend/`; neither archive may depend on stale local
`frontend/build/` output.

## Public package data

The only AV1 cold-start artifact intended for publication is:

`mediaforce/tuning/data/av1_cold_start_priors_v1.json`

It is a checked-in canonical resource loaded through `importlib.resources`.
Do not generate or replace it from a runtime database during packaging.

The sdist uses an explicit source allowlist in `pyproject.toml`. Operator
runtime config, including root `config/defaults.toml` and
`config/folder-defaults.toml`, is excluded. This prevents operator folder
rules, untracked IDE files, `.env`, `state/`, `scratch/`, runtime databases,
review media, and machine-local paths from entering an archive merely because
they exist in the checkout.

`mediaforce/package_defaults/defaults.toml` is a separate install-safe package
resource.
It contains no configured source roots, folder overrides, or machine-specific
absolute paths. Source checkouts continue to prefer root `config/defaults.toml`;
an installed wheel falls back to the package resource so its entrypoints do not
depend on a repository checkout.

## Verification

Build and verify both archives:

```bash
rm -rf dist
uv build
uv run python scripts/verify_package_contents.py dist/*.whl dist/*.tar.gz
```

The verifier requires:

- exactly one public AV1 prior in each archive
- byte identity between source, wheel, and sdist
- one byte-identical install-safe package default config
- no forbidden runtime/private archive members
- no private identifier or local-path tokens in the prior
- no current-machine or machine-specific user path in bounded scannable text
- no oversized scannable text, traversal path, or prior-path impersonator

Package verification complements the normal acceptance gate; it does not
replace backend, frontend, CLI, or PyCharm checks.

The CI `acceptance` job runs the package build and verifier, so package privacy
is a required pull-request signal rather than a manual release convention.
