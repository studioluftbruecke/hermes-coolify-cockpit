# Changelog

## 0.2.0 (2026-11-11)

Coolify Cockpit is now **Coolifybrücke**, so every Studio Luftbrücke plugin carries a
`<thing>brücke` name. Nothing else changes in what the plugin does.

- Plugin name and toolset `coolifybruecke` (was `coolify-cockpit`). The public repo is
  `github.com/studioluftbruecke/hermes-coolifybruecke`; the old URL redirects.
- Settings `COOLIFYBRUECKE_URL`, `COOLIFYBRUECKE_TOKEN`, `COOLIFYBRUECKE_READ_TOKEN` and
  `COOLIFYBRUECKE_GIT_TOKEN` (were `COOLIFY_COCKPIT_*`). The old names are not read: enter the URL
  and the tokens again under the new names.
- CLI `hermes coolifybruecke tick` and `hermes coolifybruecke alerts {on,off,status}` (was
  `hermes coolify-cockpit ...`). The alert job is named "Coolifybrücke alerts" and runs
  `coolifybruecke-alerts.py`. With alerts on, run `hermes coolifybruecke alerts on` once: it
  replaces the 0.1.0 job and deletes the old `coolify-cockpit-alerts.py`, so one job remains.
- Bundled skill `coolifybruecke:coolifybruecke` (was `coolify-cockpit:coolify-cockpit`). A morning
  digest made from the blueprint names the old skill: create it once more from the blueprint.
- The Desktop page opens at `/coolifybruecke`. The sidebar row, the page header and the settings
  title read "Coolifybrücke". The chat tab stays "Cockpit".
- The data folder moves to `plugin-data/coolifybruecke/`. On first use 0.2.0 copies each file of
  `plugin-data/coolify-cockpit/` that the new folder lacks (layout, notebook, alert state; not
  locks or caches) and leaves the old folder unchanged. This copy is removed in 0.3.0.
- The tool names stay: `coolify_capabilities`, `coolify_overview`, `coolify_deployments`,
  `coolify_diagnose`, `coolify_action`, `coolify_cockpit`, `coolify_notes`.

How to upgrade: README, "Upgrading from Coolify Cockpit 0.1.0".

## 0.1.0 (2026-11-11)

First public release, as Coolify Cockpit.
