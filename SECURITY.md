# Security Policy

## Supported Versions

Only the latest release and current `main` are supported. Fixes ship from
`main`.

## Reporting a Vulnerability

Report suspected vulnerabilities privately through GitHub's
[Report a vulnerability](https://github.com/cbusillo/mediaforce/security/advisories/new)
form. Do not open a public issue for a vulnerability.

Include the version or commit, the impact, and the smallest steps that
reproduce it.

Do not send media files, library paths, host names, encode host passwords,
or other personal data. Use redacted or made-up values.

This is a single-maintainer project. Reports are handled on a best-effort
basis, and I aim to reply within seven days.

## Scope

Relevant reports include:

- the web app letting someone replace, delete, or publish files without the
  approvals it requires;
- requests or settings that read or write outside the configured library and
  working folders;
- encode host passwords or keys leaking, or unsafe handling of media files
  and settings passed to encode hosts or FFmpeg; and
- dependency or GitHub Actions supply-chain problems.

Problems in FFmpeg or other encoders should go to those projects.
