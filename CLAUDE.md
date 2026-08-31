# rucio-sense-dmm

DMM (Data Movement Manager) brokers Rucio transfer rules onto SENSE circuits.
This repo is `groundsada/rucio-sense-dmm`, a fork of `aashayarora/rucio-sense-dmm`.

## Layout

```
src/dmm/
  api/frontend.py        FastAPI app; the hand-rolled /metrics endpoint lives here
  core/                  config, allocation, monit (Prometheus client), fts, sense
  daemons/base.py        DaemonBase; every daemon subclasses it
  daemons/core/          allocator, decider, monit, sites
  daemons/fts/           fts modifier
  models/                SQLModel tables: request, site, endpoint, mesh
```

Roughly 14 daemons run on a shared lock. Config comes from `dmm.cfg`
(`dmm.cfg.sample` is the template) via `dmm.core.config.config_get`.

## Conventions that matter

**Metrics are hand-built text, not `prometheus_client`.** `/metrics` in
`src/dmm/api/frontend.py` appends `# HELP` / `# TYPE` lines to a `list[str]` and
uses the local `_emit_gauge(lines, name, value, labels)` helper. There is no
registry and no `prometheus_client` dependency anywhere. Match that style — do
not introduce the library to add a metric.

**Cardinality is a live concern.** `/metrics` is already unbounded and
full-table-scans per scrape. Never add a metric labelled by `rule_id`, request
id, IPv6 address or anything else per-transfer. Site-level labels are fine.

**Daemons swallow their own failures.** Several paths log at DEBUG and return a
zero, which downstream code cannot distinguish from a genuine zero. When fixing
one of these, keep "query failed" and "value is legitimately 0" separate rather
than collapsing both into 0.

**There is no test suite.** `sim/unit_tests.py` covers the standalone simulator
in `sim/`, not the application. Do not claim a change is tested. Verify by
reading the call sites instead, and say plainly in the PR what was not verified.

## Working here

Never commit to `main`. Branch, then open a PR against `main` of this fork.

Commit messages: short, lowercase, subject line only. No body. Never add
`Co-Authored-By` trailers and never credit Claude, Anthropic or any AI in a
commit message, branch name or PR title.

Good: `fix: separate monit query failure from zero bytes`
Bad: `Fix: Separate monit query failure from zero bytes (generated with Claude)`

Open the PR yourself with `gh pr create`. Do not rely on the "Create PR" link
in the run summary: it prefills a body crediting Claude Code, which violates the
rule above. Write the PR title and body in the same style as a commit message —
plain, factual, no AI attribution, and state anything you could not verify.

This is a fork. `origin` is the fork, `upstream` is
`aashayarora/rucio-sense-dmm`. Keep diffs minimal and in the surrounding style,
because they may be PR'd upstream later.
