# Changelog

## 0.2.6

Update both halves: the plugin where Hermes runs and the one Hermes Desktop loads.

- New setting "Show in status bar" (on by default): off hides the Coolify chip in Desktop's status
  bar. The chip is also listed as "Coolifybrücke" in the bar's right-click menu, and a chip that
  fails to render no longer affects the rest of the bar.

## 0.2.5

A review round on security and correctness before the catalog's next review. Update both halves: the plugin where Hermes runs and the one Hermes Desktop loads.

- Requests to Coolify and requests carrying the git host token never follow a redirect: a Coolify
  address behind a login proxy now fails with an error naming where it pointed, and no token is
  ever sent to another host. A token-less read of a public repository still follows a redirect on
  the same host (a renamed repository).
- A Coolify token holding a line break (a paste across a wrap) is refused by its setting's name and
  never printed; unexpected errors report only their type.
- A login page answering in place of Coolify is an error, never "all rights held" or an all-clear;
  the alert job reports it as an outage instead of a recovery. The token check also reads no right
  from an empty answer, 204 or `{}`: Coolify answers the check's made-up id with 404 or 403.
- When Hermes cannot say whether a person is present, agent actions are refused with one sentence
  instead of reaching Hermes' auto-approval.
- Commit messages reach the agent as a masked, quoted first line marked as data.
- Pasted logs, notes and commit messages also mask `APP_KEY`, `*_MASTER_KEY`, `JWT_KEY` and other
  `*_KEY` values, and go through Hermes' own redactor as a second pass.
- The settings page's alert status starts at most one `hermes cron list` per minute (new file
  `cron-list.json`).
- A deployment id that is not id-shaped is refused before any request.
- Notes that are not text, and a layout version that is not a whole number, are refused as invalid.
- An emptied "Protected environments" field keeps `production` protected.
- The README says what "Always" on an approval prompt keeps allowing; the guide no longer says the
  0.1.0 setting names keep working.
- The page's routes in a plugin host read the profile's secrets through Hermes at most every 2
  seconds (and on Check again) instead of for every secret on every request, and never open or
  inspect `.env` themselves.
- Enter on the Cancel button of an action's confirm no longer sends the action.
- A send that fails, or fails halfway through "Redeploy N apps", no longer leaves the confirm armed: the page follows what was sent, says when a request may have run, and Check again plans only what was not sent.
- A failed action says whether its request reached Coolify. A refused connection, a redirect, a login page or a proxy's refusal in front of Coolify answers `sent: false`, and the page says the request did not run; only an answer from Coolify, or a connection that broke after the request left, may have run.
- Links from Coolify open only when they are http or https, and only through Desktop; the page never opens a window itself. Where Desktop cannot open a link, a notice names the address to open in a browser.
- The status-bar chip no longer asks the backend every 4 seconds while the plugin is off or not set up in the profile: once a minute, and while the plugin is off from every 30 seconds up to every 5 minutes.
- Followed actions, an open confirm, the diagnosis, chat chips and the banner belong to their profile; switching profiles no longer polls the other profile's apps.
- The page and its settings page say so when the Desktop half and the Hermes half run different versions.
- Cancelling a deployment refreshes its app's open deployment history at once.
- "Redeploy 1 app" instead of "Redeploy 1 apps".
- The layout routes answer 400 for a version that is not a whole number, and the notebook route refuses text that is not a string.

## 0.2.4

A "Check again" button, and a cloud icon.

- **Check again** under the setup line (the Connection widget, the settings page and the setup
  steps). One click reads the settings again, so a token saved in the settings form after the page
  opened is used at once, then runs the connection and rights check anew and shows the result and
  when it ran. While it runs, the button spins and the line says "Checking…". Before, the page
  reused a check for up to 2 minutes, so a fix outside Hermes (a token's rights in Coolify, an
  allow-list in front of it) showed only after that.
- `GET /capabilities?refresh=true` is the button's route. Without it, the route shares one check
  for 2 minutes with the agent's tool, as before.
- While the URL or every token is missing, the settings page says what to add instead of an empty
  card.
- The sidebar row and the settings entry use the codicon `cloud` instead of `dashboard`. Desktop
  takes only its built-in codicons on both places (Hermes 0.23.1), so a drawn cloud is not possible.
- A click while the page's own capabilities request is still running (page load, the 5-minute poll,
  a focus refetch) now cancels that request and checks anew. Before, the click joined it and got
  the shared check.
- The settings page's connection card says "Turn on the plugin in this profile." while the Hermes
  half is off there, shows a placeholder while the settings load, and names the error when they
  fail to load. Before, all three showed an empty card ("Coolify ?").
- A stored URL that is not valid is named on the setup line ("Not set up: the Coolify URL must
  start with http:// or https:// and name a host. Fix it in the plugin settings."), instead of
  "add the Coolify URL".
- "Checked N s ago" counts up every 15 seconds while the line is open.
- A click on **Check again** within 10 seconds of the last check answers from that check. One
  check costs about 50 Coolify requests, and Coolify allows 200 per minute per user. The agent's
  `coolify_capabilities` with `refresh` still checks at once.

## 0.2.3

Alerts use Hermes' public cron command (catalog review).

- `hermes coolifybruecke alerts on`, `alerts off`, `alerts status` and the Alerts card read and
  change the alert job through `hermes cron list --all`, `hermes cron create` and
  `hermes cron remove`, for the same profile. They no longer import Hermes' scheduler module,
  which is not part of the plugin API and can change in any Hermes update. The job itself is the
  same: no model turn, the script `coolifybruecke-alerts.py`, the name "Coolifybrücke alerts".
- When Hermes refuses the change (a schedule it does not understand, for example), `alerts on`
  prints Hermes' reason and exits with 1 instead of a Python traceback.
- A test runs the three commands against the installed `hermes`, so a changed output format
  fails the plugin's tests instead of turning alerts off without a word.

## 0.2.2

A blocked connection no longer looks like a token without rights.

- When something in front of Coolify refuses the request (a proxy or firewall with an IP
  allow-list, Coolify's API switched off, or this machine outside Coolify's allowed IPs for the
  API), the page and the tools now say "Coolify could not be reached" with that reason. Before,
  every such 403 read as "No token holds read. To unlock it: give the token the read right", even
  for a token that holds every right.
- A 403 from Coolify's own rights check ("Missing required permissions: ...") still names the
  missing right, as before.
- Page cards say "Not available: <reason>" (they said "Not available with this token setup", which
  was wrong whenever the reason was not the token).

## 0.2.1 (2026-11-11)

The sidebar row gets a switch and a position.

- **Settings → Plugins → Coolifybrücke** has a new section **Sidebar**: **Show in the sidebar**
  hides or shows the Coolifybrücke row at once, and **Position** moves it (1 to 99, lower numbers
  sit higher; Kanban is 50). With the row hidden, open the page from the command palette:
  **Open Coolifybrücke**.
- The row sits at 54 by default, in one block with the other Studio Luftbrücke plugins below
  Kanban (it was 55).
- Both choices are kept by this Desktop, not by the Hermes backend. A second computer starts
  with the row shown at 54.
- The row's label stays "Coolifybrücke" and the chat tab stays "Cockpit".
- The setup step for a profile with the plugin off says "Switch on Coolifybrücke in Capabilities,
  Plugins." (it named the slug).

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
