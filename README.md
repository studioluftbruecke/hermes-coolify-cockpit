# coolify-cockpit

Coolify Cockpit for Hermes, version 0.0.11. Status: **in development**, not released. The
connection and the token check (`coolify_capabilities`), the read tools (`coolify_overview`,
`coolify_deployments`), the failed-deploy diagnosis (`coolify_diagnose`), the actions
(`coolify_action`, behind your approval), the cockpit layout (`coolify_cockpit`, widgets and
presets), the notebook and the Desktop page (`desktop/plugin.js`) work, and the page has had its
first check inside Hermes Desktop.

A cockpit for your [Coolify](https://coolify.io) instance inside Hermes Desktop. Paste an API
token and the cockpit shows what that token may do, your servers' load, every application,
service and database, and your deployments as they run. It offers the actions the token allows
(deploy, restart, start, stop, cancel, rollback), each behind a confirmation. You arrange the
widgets yourself, start from a preset, or ask the agent to do it. When a deploy fails, the agent
classifies the log against a catalogue of common self-hosting failures and shows the evidence
lines.

The token is the permission model: a read-only token gives a read-only cockpit, and a token
scoped to your staging environment can only act on staging. Coolify already ships an MCP server
for raw API access; this plugin does not repeat it.

## Setup

You need Hermes 0.23.0 or later (`hermes --version`) and a Coolify instance that the machine
running the Hermes backend can reach. Run the commands below on that machine. To set up a profile
other than the default one, add `-p <profile>` after `hermes`.

### Install

From the plugin's own repository:

```bash
hermes plugins install <repo url> --ref <commit sha> --enable
```

From a folder inside a larger repository (a monorepo):

```bash
hermes plugins install '<repo url>#plugins/coolify-cockpit' --ref <commit sha> --enable
```

- `--ref` takes the full 40-character commit SHA and pins the install to that commit.
- `--enable` switches the plugin on in the same step. Without it, Hermes asks when it runs in a
  terminal; otherwise run `hermes plugins enable coolify-cockpit`.
- For a private repository, git's credential helper supplies the login. Never put a token in the
  URL: it ends up in the process list and in the clone's git config.
- Hermes scans every plugin from outside its catalog before it installs it, and first prints
  "Warning: custom (unreviewed) source". This plugin passes the scan, so `--force` is not needed.

The tools arrive in the next session: a chat that was already open keeps its old tool list, so
start a new one. The Desktop half ships switched off (see
[The Cockpit page in Hermes Desktop](#the-cockpit-page-in-hermes-desktop)).

### Prepare Coolify

1. In Coolify, open **Settings → Advanced**. In the **API and MCP** section, set **API access** to
   **Enabled**. With API access off, Coolify answers every token request with `403 Forbidden`.
2. In the same section, set **MCP server** to **Enabled** for server stats and the team's recent
   failures. MCP must also be on for the token's team (**Team → General**, on by default). Without
   MCP the cockpit still works, without those two, and says so.
3. If **Allowed API IPs** lists any address, add the public IP address of the machine that runs
   the Hermes backend (not your laptop, when Hermes Desktop connects to a remote backend). The list
   applies to the REST API and to `/mcp`. An empty field allows every address.
4. Switch to the team that owns your resources, then create the tokens in **Keys & Tokens → API
   tokens**.

The recommended pair:

| Token | Rights | Scope | Plugin setting |
| --- | --- | --- | --- |
| Action token | `read` + `deploy` | Only the environments the cockpit may act on, for example your staging environments | `COOLIFY_COCKPIT_TOKEN` |
| Overview token | `read` only | Team-wide | `COOLIFY_COCKPIT_READ_TOKEN` |

Leave `read:sensitive` off both tokens. It lets a token read deployment logs and secret values such
as environment variables, and anything the tools read can reach your model provider. Without it,
paste a failed log into the chat for a diagnosis. The cockpit never uses `write` or `delete`, so
neither token needs them.

### Configure the plugin

In Hermes Desktop, open **Coolify Cockpit** in the sidebar. Until the URL and a token are set, the
page shows the setup steps instead of the widgets: the address, the action token (with what to
create in Coolify and a link to its API tokens page) and the optional overview token. Each step's
button opens the plugin's settings form for that profile (**Settings → Plugins → coolify-cockpit**,
also reachable from **Capabilities → Plugins → coolify-cockpit**, the gear). The page notices a
saved value within a few seconds, without a restart, and then shows your layout.

| Setting | What to enter |
| --- | --- |
| Coolify URL (`COOLIFY_COCKPIT_URL`) | The address you open Coolify at |
| Action token (`COOLIFY_COCKPIT_TOKEN`) | A Coolify API token (Coolify: Keys & Tokens → API tokens). Recommended: `read` + `deploy`, scoped to the environments the cockpit may act on |
| Overview token (`COOLIFY_COCKPIT_READ_TOKEN`, optional) | A team-wide token with only `read`, for server stats and the full resource list |
| Check for Coolify updates (`check_updates`) | On by default |
| Let the agent run actions (`agent_actions`) | On by default. Every agent action still asks for your approval; off means only your own clicks act |

Any setup works: one token or two, scoped or team-wide, read-only or full access. Ask Hermes
"what can the Coolify cockpit do with my token?" and it answers from `coolify_capabilities`:

| Setup | Unlocks | Not available, because ... |
| --- | --- | --- |
| Recommended: scoped `read` + `deploy` action token, team-wide `read` overview token | Everything except live logs | Live logs need `read:sensitive`; paste failed logs instead |
| One scoped `read` + `deploy` token | Resources, deployments and actions inside the scope | No server stats (scoped tokens cannot read servers); only the projects in scope are visible |
| One team-wide `read` token | Read-only cockpit with server stats | No actions (no `deploy`), no live logs |
| One team-wide `read`, `read:sensitive`, `deploy` token | Everything | Nothing; it warns that it can stop production and that logs can reach the model |
| A token with `write` or `delete` | Whatever its other rights unlock | The cockpit never uses `write` or `delete` and warns that the token holds them |

Reads use the token with the widest view. Actions use only the action token (or your only
token), never the overview token, even if the overview token could.

### Configure without Hermes Desktop

The settings form writes two places, and you can write both yourself. The URL and the two switches
go into the profile's `config.yaml`:

```bash
hermes config set plugins.entries.coolify-cockpit.settings.COOLIFY_COCKPIT_URL https://coolify.example.com
```

The switches take `true` or `false` the same way, for example
`hermes config set plugins.entries.coolify-cockpit.settings.check_updates false`.

The tokens go into the profile's `.env`, whose path `hermes config env-path` prints. Type each token
at a hidden prompt, so it never shows on screen, in your shell history or in the process list (the
lines work in bash and zsh; for another profile, add `-p <profile>` to `hermes config env-path`):

```bash
printf 'Action token: '; read -rs t && printf 'COOLIFY_COCKPIT_TOKEN=%s\n' "$t" >> "$(hermes config env-path)"; unset t; echo
printf 'Overview token: '; read -rs t && printf 'COOLIFY_COCKPIT_READ_TOKEN=%s\n' "$t" >> "$(hermes config env-path)"; unset t; echo
```

Skip the second line if you use one token. To replace a token, delete its old line from `.env`
first, so the file holds each name once. Never pass a token as a command-line argument or to
`hermes config set`: an argument stays in your shell history, and a secret does not belong in
`config.yaml`.

### Check the setup

In a new session, `hermes plugins list` shows `coolify-cockpit` as enabled, and
`hermes -z "What can the Coolify cockpit do with my token?"` answers from `coolify_capabilities`.

## Good to know

- **`@coolify:` references send live data.** Any literal `@coolify:<value>` in a message you send
  is expanded with live data from your instance when the message is sent, and that data goes to
  your model provider with the message. This also happens when you paste text from an earlier
  reply that contains such a reference. In the composer a reference shows as plain text, not as a
  chip.
- **The cockpit chat is one tab.** "Ask" and the chat widget open the Hermes session titled
  **Cockpit** as a tab and reuse that tab for the next question.
- **The first load after a start is slow.** The first page load after the server's plugin host
  starts (after Hermes starts or restarts) can take several seconds; later loads answer in well
  under a second. While it waits, the page says that the cockpit backend is starting.
- **A token saved, changed or removed later is used at once.** The agent's tools read the token on
  every call. When the page's routes run in a profile's plugin host, whose environment is fixed
  when it starts, they read the plugin's own secrets (`COOLIFY_COCKPIT_*`) through Hermes'
  secret-scope builder on every request instead.
- **Disabling the plugin stops every action at once.** After `hermes plugins disable
  coolify-cockpit`, Hermes keeps the hooks the plugin already registered (the approval check for
  `coolify_action`) until the gateway restarts. The tools are gone, so no action can run.

## Tools

The tools form the toolset `coolify-cockpit`. Name it to limit a run to them
(`hermes chat -t coolify-cockpit`) or to give them to a chat platform in `platform_toolsets`.

| Tool | What it does |
| --- | --- |
| `coolify_capabilities` | Instance version and available update, each token's rights and scope and what it sees, the features your setup unlocks and those it does not (with the reason and what would unlock them), warnings |
| `coolify_overview` | One compact snapshot: servers with current CPU, RAM, disk, load and network; every application, service and database with project, environment, status and domain; deployments running now; the most recent failed deployments. Filters by project or environment. Cached for 10 seconds |
| `coolify_deployments` | Deployment history of one application (by name or id), or the details of one deployment. Never includes build logs |
| `coolify_diagnose` | A failed deployment's log, pasted by you or fetched by deployment UUID (needs `read:sensitive`), classified against a catalogue of common self-hosting failures: the class, a confidence, the evidence line numbers, a likely fix and the relevant log lines |
| `coolify_action` | One lifecycle action: `deploy`, `force_deploy`, `restart`, `start`, `stop`, `cancel` (a queued or running deployment) or `rollback` (needs `params.commit`). Returns Coolify's answer and the deployment id where Coolify creates one |
| `coolify_cockpit` | Reads and rearranges the cockpit page: `get`, `add_widget`, `remove_widget`, `update_widget`, `move_widget`, `apply_preset`. It edits only the layout; it never runs a Coolify action |
| `coolify_notes` | Read the instance notebook `COOLIFY.md` (newest entries first within a size cap) or append one dated entry |

The diagnosis catalogue covers: the build running out of memory, a build variable that was empty
or missing, a failed health check (wrong port, localhost bind), a health check without curl or
wget in the image, a full disk or stuck build cache, a Node.js version mismatch, a private registry
or package that refused the credentials, a repository clone that was refused, conflicting Traefik
labels, an outdated lockfile, and a lost SSH connection to the server. Anything else comes back as
`unknown` with the log's error lines. The classifier is plain pattern matching, no model call; the
catalogue was built from failed deployment logs posted publicly in Coolify's issue tracker.

### Actions and your approval

Every action needs the action token's `deploy` right and a target that token can see; the
overview token is never used for an action, even if it holds `deploy`. Targets are matched by
exact name or id only, never by part of a name. `stop` always sends `docker_cleanup=false`:
Coolify's default would run a Docker cleanup on the whole server.

When the agent calls `coolify_action`, Hermes shows its approval prompt before anything is sent,
naming the action, the resource, its project, environment and server, and the request. Deny, and
nothing reaches Coolify. In runs where no one can answer (cron jobs, webhooks, the API server,
single-query `hermes chat -q` or `-z`), the call is refused without asking and without any request
to Coolify; the read tools keep working there. If you run plugins in a separate plugin host
(`plugins.isolation: host`), the plugin cannot see whether a CLI turn is a one-shot `hermes -z`
run, so it also refuses agent actions from the CLI there; Desktop, the TUI and chat platforms
still ask. If you switch Hermes' approvals off or turn on
`/yolo`, Hermes approves without asking, as it does for every other gated tool.

Where the turn runs decides who can approve:

| Where the agent runs | What `coolify_action` does |
| --- | --- |
| Hermes Desktop, the TUI, `hermes chat` in a terminal | Hermes shows its approval prompt. Nothing is sent until you allow it |
| A chat platform such as Telegram | Hermes posts the approval prompt into that chat, and anyone there who may answer approvals can allow it. Without an answer within `approvals.timeout` (300 seconds by default), the call is denied and nothing is sent |
| Cron jobs | Refused without asking, no request to Coolify |
| Webhooks and the API server | Refused without asking, no request to Coolify |
| `hermes chat -q` and `hermes -z` | Refused without asking, no request to Coolify |

### Arranging the cockpit

The cockpit is a list of widgets, top to bottom, each with a size (`s`, `m`, `l`) and its own
settings. Start from a preset, change it in the page, or ask Hermes ("put a Deploy desk for staging
at the top with a restart button for my-app-staging"); the page and the agent edit the same layout.

| Widget | Shows | Needs |
| --- | --- | --- |
| `connection` | Reachability, version, each token's rights, what your setup unlocks | any accepted token |
| `servers` | Server health, load and a history chart | a team-wide `read` token (stats: MCP and Sentinel metrics on) |
| `resources` | Applications, services, databases; filters; optional row buttons | `read` |
| `deployments` | Running deployments and recent failures | `read` |
| `quick_actions` | Pinned buttons, each one action on one resource | the action token's `deploy` |
| `diagnosis` | Failed deployments with a diagnosis card and a paste box | `read` (live logs: `read:sensitive`) |
| `update_notice` | A newer Coolify release, with a link to your instance | the update check on |
| `notebook` | Your instance notebook | nothing |
| `backups` | Backup schedules and the last run per database | `read` |
| `chat` | Ask about what you are looking at | nothing |

Presets: **Overview** (the default: a deploy desk for the `staging` environment with deploy and
restart buttons, servers, deployments, resources, and connection and rights last), **Deploy desk**
(one environment's resources with deploy and restart buttons, its deployments and diagnosis, and
optional quick-action buttons on top) and **Server watch** (servers with history, the update
notice, the notebook). A preset can replace the layout or join it at the top or bottom.

Unknown widgets or settings are refused with the list of known ones. Quick-action buttons name an
action from the same list `coolify_action` uses (`cancel` is not pinnable; it acts on one
deployment); the resource is looked up only when you click, inside the action token's view, and
the click asks for confirmation. Every save carries the layout's version: if the agent changed the
layout while you were editing it (or the other way round), the older save is refused and you get
the current layout back, so neither edit is lost silently.

### The Cockpit page in Hermes Desktop

The plugin's Desktop half adds a **Coolify Cockpit** row to the sidebar, a status-bar chip (a health
dot, deployments running, failures in the last 24 hours, an arrow when a Coolify update is out;
"Coolify: setup needed" before the setup is done) and the command **Open Coolify Cockpit**. It ships switched off: after installing, turn it on in
**Capabilities → Plugins**.

The page draws your saved layout as a grid of widgets: connection and rights (per token, plus what
your setup unlocks and what it does not, with the reason), servers (metrics with a one-hour line),
resources (filters, row actions, deployment history), deployments (running and recent failures,
links into Coolify), quick actions, diagnosis (pick a failed deployment; without `read:sensitive`
you paste its log), the update notice, the notebook (read and add entries), and the cockpit chat.
A widget your token setup cannot fill says why and what would unlock it.

Every action button opens a confirmation naming the action, the resource, its environment and
server, the token and the request; only then is it sent, and the page follows the deployment until
it ends. **Edit layout** lets you add, remove, reorder and resize widgets, change their settings
and apply a preset; a save that collides with a newer version (the agent's) is refused, and the
page reloads the newer layout and tells you.

"Ask" on a server, resource or deployment, and the chat widget, open a normal Hermes chat titled
**Cockpit** as a tab, with your question drafted and an `@coolify:` reference for the item; the
next question reuses that tab. Nothing is sent until you press send; the chat uses your
configured provider and model.

The page reads the snapshot every 60 seconds (every 15 while a deployment runs or an action you
sent is being followed), the layout every 15 seconds (so the agent's edits appear), and the token
check and notes every few minutes, only while Hermes Desktop is open. When the backend has not
answered after 2.5 seconds on the first load, the page says that it is starting.

### The instance notebook (COOLIFY.md)

`coolify_notes` reads and appends to `COOLIFY.md`, a plain Markdown notebook about your instance in
the plugin's data folder: facts that neither Hermes memory nor a skill holds ("shop-web needs
`NPM_TOKEN` as a build variable", "upgraded 4.6.0 → 4.7.0"). You write it from the cockpit, the
agent writes it after it learns something (a diagnosis), and the cockpit adds its own dated entry
when it sees the Coolify version change, a server appear or disappear, or a token's rights or scope
change (compared with the last state it saw, kept in `observed-state.json`). Each entry is dated and
signed `you`, `agent` or `cockpit`; writes are atomic, and known secret formats are masked before
anything is stored. Hermes reads the notebook only when asked (`coolify_notes read` or
`@coolify:notes`), never in every turn.

### Update notice

`coolify_capabilities` and `coolify_overview` say in one line which Coolify version runs and whether
a newer release is out, with the link to your instance; the upgrade itself stays a click in Coolify.
The overview reads the instance's version at most every 10 minutes. The overview, the tool and the
page share one read of Coolify's release feed at most every 6 hours (`version-check.json` in the
data folder).

### For the agent: skill, `@coolify:` references, redaction

- A bundled skill, `coolify-cockpit:coolify-cockpit`, teaches the agent which tool answers which
  question, the debug order (deployments, then details, logs last), the diagnosis catalogue and the
  safety rules.
- Context references put live state into a message in one token: `@coolify:overview` (the snapshot
  summary), `@coolify:<resource name>` (`@coolify:"name with spaces"`), `@coolify:deployment/<uuid>`,
  `@coolify:server/<uuid>` and `@coolify:notes`. Each expands to at most 4000 characters, read with
  the same tokens and 10-second cache as the tools; a name that matches nothing says so and lists
  what the token can see. Hermes only expands the `@prefix:value` form, so a bare `@coolify` stays text.
- The token formats the log redaction knows (GitHub, GitLab, npm, Slack, AWS, Google, Stripe,
  Anthropic, OpenAI, SendGrid, DigitalOcean, JWTs) are registered with Hermes' own redaction, so
  Hermes masks them in its logs and tool output too. Coolify's own token format starts with digits,
  which Hermes' rules do not accept; the plugin masks it in logs itself.

### Backend routes

For the Desktop page, under `/api/plugins/coolify-cockpit/`: `GET /setup` (which settings are
filled in, no Coolify request), `GET /capabilities`,
`GET /snapshot` (`?refresh=true` skips the cache) and `GET /resources/{uuid}/deployments`
(`?take=N`, up to 50) return the same data as the tools, as JSON. `GET /actions` lists the actions
and whether the action token holds `deploy`; `POST /actions` with `{kind, target, params?,
dry_run?}` runs one action for the page's own buttons (`dry_run: true` returns the request and the
confirmation text without sending anything). The page confirms each click first; the route runs
the same rights and view checks as the tool. `GET /layout` returns the layout with the widget types
and presets (`?availability=true` adds, per widget, whether your token setup unlocks it and why
not); `PUT /layout` with `{expected_version, layout}` saves the page's edit and answers 409 with the
current layout when it changed in between; `POST /layout/preset` with `{preset, params?,
placement?, expected_version?}` applies a preset. `GET /notes` (`?max_chars=N`) returns the
notebook, newest entries first within the cap; `POST /notes` with `{text, topic?}` adds your entry.
`POST /diagnose` with `{log_text}` (a pasted log) or `{deployment_uuid}` (fetched only with a
`read:sensitive` token; otherwise the answer asks for a paste) returns the diagnosis card the page
shows, with secrets masked.

**A changed token is used at once, without a restart.** Chats read the token on every call. For a
profile whose routes run in its plugin host process (`HERMES_PLUGIN_HOST_PROCESS=1`, every
profile but the one the backend launched as), the host's environment is the profile's `.env` as it
was when the host started, so each route request reads the plugin's own secrets
(`COOLIFY_COCKPIT_*`) through Hermes' secret-scope builder (`agent.secret_scope.
build_profile_secret_scope`), the same one a gateway turn uses; a token removed from the settings
is gone at once there too. In the backend's own process the request already runs in a fresh
profile scope and the routes use `get_secret`. `GET /setup` says which settings are filled in,
without a Coolify request and without a token value. Route errors that a wrong token could explain say so in a `hint` field.

### What a snapshot costs

One snapshot reads, in parallel: `GET /servers` per token (to tell scoped from team-wide),
`/applications`, `/services`, `/databases`, `/deployments` (the running queue), `/projects` and
one `/projects/{uuid}` per project (environment names; reused for 10 minutes unless a resource
sits in an environment they do not name), and through Coolify's MCP endpoint
`get_server_metrics` per server with Sentinel metrics on and one `list_deployments` for the
team's recent failures. Without MCP the snapshot still answers, without metrics and recent
failures, and says so. Per-application history is read only when you ask for it.

## Disclosures

### Network calls

The plugin talks to two hosts: the Coolify instance you configure, and Coolify's public release
feed. It calls nothing else.

Every request to Coolify goes to `<your Coolify URL>/api/v1/...` or `<your Coolify URL>/mcp`, with
one of your tokens as a Bearer token:

| When | Requests |
| --- | --- |
| Token check (`coolify_capabilities`, the page's connection widget and its availability check) | Per token: `GET /applications/<made-up id>`, `GET /applications/<made-up id>/logs`, `POST /deploy?uuid=<made-up id>`, `PATCH /applications/<made-up id>` and `DELETE /applications/<made-up id>` (the right probes, below); `GET /servers`, `GET /projects`, `GET /projects/{uuid}` per project, `GET /applications`, `GET /services` and `GET /databases`. Once: `GET /version` and an MCP `initialize` |
| Snapshot (`coolify_overview`, `@coolify:` references, the page and its status-bar chip) | `GET /servers` per token, `GET /applications`, `GET /services`, `GET /databases`, `GET /deployments` (the running queue), `GET /projects` and `GET /projects/{uuid}` per project (at most every 10 minutes, or when a new environment appears); MCP `tools/call` `get_server_metrics` per server with Sentinel metrics on and one `list_deployments` (recent failures). About 30 requests on an instance with 23 projects the first time, 6 afterwards |
| Deployment history and details (`coolify_deployments`, the page) | `GET /deployments/applications/{uuid}?skip=0&take=N`, `GET /deployments/{uuid}` |
| Action list (the page) | With the action token only: the `deploy` probe |
| Failed-deploy log (`coolify_diagnose`, only with a `read:sensitive` token) | Two read-right probes against the made-up id, then `GET /deployments/{uuid}` |
| Actions (`coolify_action` after your approval, or your click in the page) | With the action token only: the `deploy` probe, `GET /applications`, `GET /services`, `GET /databases` or `GET /{kind}s/{uuid}` to find the target in that token's view (`GET /deployments/{uuid}` for `cancel`; that view is kept for 60 seconds, so the send after the page's check, or after your approval, reads nothing again), then one `POST` to `/deploy?uuid=...` (`&force=true` for a force rebuild), `/applications/{uuid}/restart`, `/services/{uuid}/restart`, `/databases/{uuid}/restart`, the same three with `/start` and `/stop` (`stop` always sends `docker_cleanup=false`), `/applications/{uuid}/rollback` or `/deployments/{uuid}/cancel` |

The snapshot and history code accepts only `GET` requests and the MCP tools `get_server_metrics`
and `list_deployments`; a guard refuses anything else.

**Coolify's API rate limit.** Coolify allows `API_RATE_LIMIT` requests per minute (200 by default) per Coolify user, shared by every token of that user, with 403 and 404 answers counted too (MCP calls are not). An open Cockpit page with its chip uses about 80 in the minute it opens, about 6 per minute while idle (56 in the minute of the 5-minute token check) and about 36 per minute while it follows a deployment, measured against an instance with 23 projects and 50 resources; before 0.0.10 that was 129, 30 and 132. When Coolify answers 429 the plugin says "Coolify is limiting API requests; try again in N s" and never retries on its own.

How rights are detected: Coolify checks a token's right before it looks up the resource, so the
right probes go to the made-up id `hermes-permission-probe-does-not-exist`. No real resource
receives a write from this check. Coolify answers "not found" for a right the token holds and
"missing permission" for one it lacks.

With `check_updates` on (the default), the plugin sends `GET
https://cdn.coollabs.io/coolify/versions.json`, without any token or instance data, at most once
every 6 hours. The tool, the overview and the page share that answer through `version-check.json`.

### When the calls happen

- The agent side has no background loop. It calls Coolify only when a tool runs or a message with
  an `@coolify:` reference is sent.
- The tools and the page share one snapshot for 10 seconds (`snapshot-cache.json`), and server
  metrics for 60 seconds. The overview reads the instance version at most every 10 minutes.
- The token check (`coolify_capabilities`, the page's connection widget and its availability
  check) runs at most once every 2 minutes for all of them together (`capabilities-cache.json`);
  `coolify_capabilities` with `refresh` checks again at once.
- A followed deployment's history looks its application up in the last snapshot, so polling it
  every 5 seconds reads only that history.
- With the Desktop half switched on, Hermes Desktop polls only while it is open. The status-bar
  chip reads the snapshot every 60 seconds and the token check every 5 minutes. The open Cockpit
  page shares those reads and adds: the snapshot every 15 seconds while a deployment runs or an
  action you sent is followed, a followed deployment's history every 5 seconds until it ends, the
  availability check and the action list every 5 minutes, and the layout (every 15 seconds) and
  the notebook (every 60 seconds), which are files on the Hermes backend and call no Coolify
  endpoint.

### Credentials

One or two Coolify API tokens in the profile's `.env` (written by the plugin settings form, or by
you at a hidden prompt), read at call time. The plugin never reads `COOLIFY_API_TOKEN` or
`COOLIFY_BASE_URL` (Coolify
sets those inside the containers it runs). Tool output names a token only by its role and the
first 8 hex digits of its SHA-256. Any token stored for Hermes is readable by whatever else runs as
Hermes on that host, so scope tokens narrowly (see the recommended pair in Setup).

### Actions

Every agent call goes through Hermes' approval prompt; unattended runs refuse without any request.
The page's route acts on your click without a Hermes prompt, so anything on the host that can call
Hermes' local API as you can call it too: scope the action token to what you would let it touch.

### Logs and pasted text

`coolify_diagnose` reads a log only when you paste it or when a token holds `read:sensitive`. Log
text is untrusted: secrets matching known formats (API tokens, bearer tokens, passwords in URLs,
`KEY=value` with secret-looking names, the environment variables Coolify stores as one base64
value, private keys) are masked first, and the lines the agent sees sit inside a block marked as
data with a random nonce. Masking covers
known formats only, so the log lines that are shown can still reach your model provider. So can the
live data an `@coolify:` reference adds to a message.

### Files

Everything the plugin writes lives in its data directory in the Hermes profile
(`plugin-data/coolify-cockpit/`):

| File | Holds |
| --- | --- |
| `layout.json` | The cockpit layout: widget types, sizes, settings and pinned resource names, with version, author and time of the last save. Edit it by hand if you like; it is checked on load |
| `layout.json.lock` | An empty lock file, so the page and the agent never save at the same moment |
| `COOLIFY.md` | The instance notebook |
| `.COOLIFY.md.lock` | An empty lock file for the notebook and `observed-state.json` |
| `snapshot-cache.json` | The last snapshot: names, statuses, domains, server metrics, deployments; the token only by its fingerprint |
| `version-check.json` | The last instance version, the latest release, and when each was read |
| `capabilities-cache.json` | The last token check as the tool reports it (rights, scope, what each token sees, features), for 2 minutes; tokens only by fingerprint |
| `observed-state.json` | The last seen Coolify version, server names and ids, and each token's rights, scope and fingerprint (never the token), to notice changes for the notebook |

Writes go to a temporary file beside the target (`.<name>.<process id>.<...>.tmp`) that replaces it
in one step. Apart from its own install folder, the plugin reads no other files; Hermes hands it
the settings and tokens.

### Desktop half

It talks only to this plugin's own routes (through Hermes) and to Hermes' session calls for the
cockpit chat (create a session titled "Cockpit", open it as a tab, fill its draft; it never sends).
It stores one value on your machine, the Cockpit chat's session id per profile, in Hermes Desktop's
plugin storage. Links open in your browser.

### Not included

No telemetry or analytics, no shell commands, no background processes, no inbound port.

## License

MIT, see `LICENSE`.
