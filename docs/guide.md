# Coolifybrücke guide

The full reference for Coolifybrücke: preparing Coolify, every setting and token setup, the
attention list, alerts, the tools, actions and approvals, the page, and what each read costs.
The [README](../README.md) has the short version and every disclosure.

## Setup in detail

### Prepare Coolify

1. In Coolify, open **Settings → Advanced**. In the **API and MCP** section, set **API access** to
   **Enabled**. With API access off, Coolify answers every token request with `403 Forbidden`.
2. In the same section, set **MCP server** to **Enabled**. The cockpit reads server stats, the
   failing apps and the latest deploys through it. MCP must also be on for the token's team
   (**Team → General**, on by default). Without MCP the cockpit still works: it has no server
   stats, it reads each app's history every 5 minutes instead of the two lists, and it says so.
3. If **Allowed API IPs** lists any address, add the public IP address of the machine that runs
   the Hermes backend (not your laptop, when Hermes Desktop connects to a remote backend). The list
   applies to the REST API and to `/mcp`. An empty field allows every address.
4. Switch to the team that owns your resources, then create the token in **Keys & Tokens → API
   tokens**.

The recommended setup is one token:

| Token | Rights | Scope | Plugin setting |
| --- | --- | --- | --- |
| API token | `read` + `deploy` | All projects | `COOLIFYBRUECKE_TOKEN` |

Two optional tokens exist for the cases that need them:

| Token | When | Rights | Plugin setting |
| --- | --- | --- | --- |
| Read-only token | Only when your API token is limited to some projects. A limited token cannot read servers and sees nothing outside its projects. A team-wide read token shows servers and every app, and the cockpit never acts with it | `read`, team-wide | `COOLIFYBRUECKE_READ_TOKEN` |
| Git host token | Only for "branch ahead" on private repositories when Forgebrücke is not installed (see [Branch ahead](#branch-ahead)) | Read-only repository access on your git host | `COOLIFYBRUECKE_GIT_TOKEN`, with the setting `git_host` |

The env names are the same as in 0.0.x, so an existing setup keeps working without re-entering a
token.

Leave `read:sensitive` off the Coolify tokens. It lets a token read deployment logs and secret
values such as environment variables, and anything the tools read can reach your model provider.
Without it, paste a failed log into the chat for a diagnosis. The cockpit never uses `write` or
`delete`, so no token needs them.

### Configure the plugin

In Hermes Desktop, open **Coolifybrücke** in the sidebar. Until the URL and a token are set, the
page shows two setup steps instead of the widgets: **Coolify URL** and **API token**, each one line
and one button (**Enter URL**, **Paste token**, and **Open API tokens in Coolify** once the URL is
set). The buttons open the plugin's settings (**Settings → Plugins → Coolifybrücke**, with the
same icon as the sidebar row). The page notices a saved value within a few seconds, without a
restart, and then shows your layout.

After the token is saved, one result line takes the place of the explanation, for example
"Team-wide · read, deploy · 28 apps · 2 servers". A limited token shows "Limited to 5 projects ·
read, deploy · no servers" and a button **Add a read-only token**. A token that holds `write` or
`delete` gets an amber chip "can change and delete".

The settings form, in its order:

| Setting | What to enter |
| --- | --- |
| Coolify URL (`COOLIFYBRUECKE_URL`) | The address you open Coolify at |
| API token (`COOLIFYBRUECKE_TOKEN`) | A Coolify API token (Coolify: Keys & Tokens → API tokens). Recommended: `read` and `deploy`, all projects |
| Protected environments (`protected_environments`) | Comma-separated environment names. Default `production` |
| Agent may act on protected environments (`agent_on_protected`) | Off by default. See [Protected environments](#protected-environments) |
| Let the agent run actions (`agent_actions`) | On by default. Every agent action still asks for your approval; off means only your own clicks act |
| Disk warning at (%) (`disk_warn_percent`) | Default 85. A server at or over this disk use needs attention |
| Memory warning at (%) (`memory_warn_percent`) | Default 90. A server at or over this memory use needs attention |
| Check for Coolify updates (`check_updates`) | On by default |
| Read-only token (optional) (`COOLIFYBRUECKE_READ_TOKEN`) | A team-wide token with only `read`. Only if the API token is limited to some projects |
| Git host token (optional) (`COOLIFYBRUECKE_GIT_TOKEN`) | A read-only token for your git host |
| Git host of that token (optional) (`git_host`) | Where the git host token is sent, for example `git.example.com`. Leave it empty for a GitHub token |

Any setup works: one token or two, limited or team-wide, read-only or full access. Ask Hermes
"what can Coolifybrücke do with my token?" and it answers from `coolify_capabilities`:

| Setup | Unlocks | Not available, because ... |
| --- | --- | --- |
| Recommended: one team-wide `read` + `deploy` API token | Everything except live logs | Live logs need `read:sensitive`; paste failed logs instead. It warns that the token can deploy, restart and stop every resource |
| A limited `read` + `deploy` API token and a team-wide `read` read-only token | Everything except live logs; actions only inside the API token's projects | Live logs, as above |
| One limited `read` + `deploy` token | Resources, deployments and actions inside its projects | No server stats (a limited token cannot read servers); only the projects in its scope are visible |
| One team-wide `read` token | Read-only cockpit with server stats | No actions (no `deploy`), no live logs |
| One team-wide `read`, `read:sensitive`, `deploy` token | Everything | Nothing; it warns that it can stop production and that logs can reach the model |
| A token with `write` or `delete` | Whatever its other rights unlock | The cockpit never uses `write` or `delete` and warns that the token holds them |

Reads use the token with the widest view. Actions use only the API token (or your only token),
never the read-only token, even if the read-only token could.

### Configure without Hermes Desktop

The settings form writes two places, and you can write both yourself. The URL, the environment
list, the switches and the thresholds go into the profile's `config.yaml`:

```bash
hermes config set plugins.entries.coolifybruecke.settings.COOLIFYBRUECKE_URL https://coolify.example.com
```

The other settings take their value the same way, for example
`hermes config set plugins.entries.coolifybruecke.settings.check_updates false` or
`hermes config set plugins.entries.coolifybruecke.settings.protected_environments "production, shop-live"`.

The tokens go into the profile's `.env`, whose path `hermes config env-path` prints. Type each token
at a hidden prompt, so it never shows on screen, in your shell history or in the process list (the
lines work in bash and zsh; for another profile, add `-p <profile>` to `hermes config env-path`):

```bash
printf 'API token: '; read -rs t && printf 'COOLIFYBRUECKE_TOKEN=%s\n' "$t" >> "$(hermes config env-path)"; unset t; echo
```

Only if you need them, the optional tokens the same way:

```bash
printf 'Read-only token: '; read -rs t && printf 'COOLIFYBRUECKE_READ_TOKEN=%s\n' "$t" >> "$(hermes config env-path)"; unset t; echo
printf 'Git host token: '; read -rs t && printf 'COOLIFYBRUECKE_GIT_TOKEN=%s\n' "$t" >> "$(hermes config env-path)"; unset t; echo
```

To replace a token, delete its old line from `.env` first, so the file holds each name once. Never
pass a token as a command-line argument or to `hermes config set`: an argument stays in your shell
history, and a secret does not belong in `config.yaml`.

### Check the setup

In a new session, `hermes plugins list` shows `coolifybruecke` as enabled, and
`hermes -z "What can Coolifybrücke do with my token?"` answers from `coolify_capabilities`.

## What needs you: the attention list

Every count the cockpit shows comes from one list. The summary row, the status-bar chip, the app
badges, `coolify_overview`, `@coolify:overview` and the alert job all read it. Each entry comes
from one signal:

| Signal | Label | Severity | When | Fix it offers |
| --- | --- | --- | --- | --- |
| `failing` | failing | bad | The app's newest finished-or-failed deployment failed and none is running. A cancelled deployment counts as nothing | Roll back to the last good commit, or Deploy when none is known |
| `unhealthy` | unhealthy | bad | Running but failing its health check, degraded, or restarting. A stopped resource does not count: it is often stopped on purpose | Restart |
| `server_down` | server down | bad | A server is not reachable or not usable | |
| `server_pressure` | server pressure | warn | Disk at or over `disk_warn_percent` (85), or memory at or over `memory_warn_percent` (90) | |
| `env_changed` | env changed | warn | An env variable changed after the app's last finished deployment | Deploy |
| `behind_branch` | branch ahead | warn | The app's branch has commits that are not deployed. The app badge reads "N ahead", or "ahead" when the count is unknown | Deploy |
| `update_available` | update | info | A newer Coolify release exists | |

`failing` reads the team's newest 100 finished and 100 failed deployments in two MCP calls. An
app whose newest result is older than one of those lists reaches gets one REST history read before
it counts, and that answer is kept until the app deploys again.

`env_changed` compares each env row's `updated_at` with the last finished deployment. It reads one
`GET /applications/{uuid}/envs` per app, again after 10 minutes or after the app's next finished
deployment, at most 6 per served snapshot. It stores timestamps only, never a value. It covers env
variables, not domain or build-setting edits, so the badge says "env changed".

A backup signal is not included: the instance this was built against has no database, so the
shape of a backup run could not be read.

### Branch ahead

Coolify does not know a branch's head: every application stores `git_commit_sha: HEAD`. The
cockpit asks the git host, through the first source that can answer, in this order:

1. **Forgebrücke**, when it is enabled in the same profile and the repository is on its Forgejo
   host. The cockpit calls Forgebrücke's `forgejo_get` (`kind=branch`) in-process, and Forgebrücke
   uses its own token. Only the alert tick and the agent's tool calls can make that call; the page
   reads the answers they stored. Forgebrücke returns the head only, so these apps show "ahead"
   without a count.
2. **The git host token**, when the repository is on `git_host`, or on github.com when `git_host`
   is empty and the token looks like a GitHub token (`ghp_`, `github_pat_`, `gho_`, `ghs_`,
   `ghu_`). It reads the branch and counts the commits through the host's compare API (Forgejo or
   Gitea `/api/v1`, or `api.github.com`). The token goes to no other host.
3. **Git's smart HTTP without a credential** (`info/refs?service=git-upload-pack`). It answers for
   public repositories only. A private one answers 401 and is asked again after 6 hours.

A head is read again after 10 minutes, or at once when the deployed commit changes. At most 8 apps
are read per served snapshot (40 per alert tick). The answers live in `branch-heads.json`: repository,
branch and commit SHAs only. To fix "branch ahead", deploy the app.

## Alerts with Desktop closed

The page polls only while Hermes Desktop is open. For alerts without it, turn on a Hermes cron job
that runs without a model turn:

```bash
hermes coolifybruecke alerts on                       # every 5 minutes, output kept in Hermes
hermes coolifybruecke alerts on --every 15m --deliver telegram
hermes coolifybruecke alerts status
hermes coolifybruecke alerts off
```

- `alerts on` writes the script `<profile>/scripts/coolifybruecke-alerts.py` and creates one cron
  job, "Coolifybrücke alerts", with no model turn, that runs it. `--every` takes `5m`, `15m`,
  `1h` and similar (default `5m`). `--deliver` takes `local` (the default: the job's output stays
  in Hermes) or a messaging platform you have connected in Hermes. Running `alerts on` again
  replaces the job instead of adding a second one.
- `alerts off` removes the job and the script. `alerts status` says whether the job is on, how
  often it runs, where it delivers, when it last checked and how many items are open.
- The script runs `hermes coolifybruecke tick`. You can run it yourself: `--peek` shows what is new
  without marking it as announced, and `--json` adds the facts as JSON.

What a run prints:

- The first run lists everything that needs you now. After that, a run prints only new items and a
  "Recovered" line for each item that cleared. A run with nothing new prints nothing, and Hermes
  then delivers nothing.
- An item stays open while its app is deploying or failing: a redeploy is not a recovery yet.
- `unhealthy` and `server_pressure` need two runs in a row to alert, and two runs without them to
  recover. Both flicker between runs a few minutes apart (measured).
- Coolify not reachable, or a rejected token, is an item of its own. A rate-limited run changes
  nothing.
- The state lives in `alerts-state.json`, keyed by the instance URL, so a rotated token does not
  announce everything again.
- One run reads up to 40 env and branch answers, enough for 28 apps in one run.

**From a chat.** The Alerts card in **Settings → Plugins → Coolifybrücke** shows whether the job is
on, and its button **Ask Hermes to turn on** (or **Ask Hermes to turn off**) drafts the request into
the Cockpit chat; nothing is sent until you press send. The agent then runs the command, or, for
alerts in that chat, creates the same job with its cron tool (`no_agent`, script
`coolifybruecke-alerts.py`, deliver `origin`) after `alerts on` wrote the script. Delivery
`origin` from a Desktop chat writes a note into that chat. A push to your phone with Desktop closed
needs a messaging platform connected in Hermes.

### Morning digest

The plugin registers an automation blueprint, **Coolify morning digest**
(`coolifybruecke:morning-digest`). It asks for a time (default 08:00) and a delivery target
(default `origin`). Each morning it runs one model turn that calls `coolify_overview` once and writes
at most 8 lines: what needs you, yesterday's deploys, and servers under pressure. It never acts.

## Tools

The tools form the toolset `coolifybruecke`. Name it to limit a run to them
(`hermes chat -t coolifybruecke`) or to give them to a chat platform in `platform_toolsets`.

| Tool | What it does |
| --- | --- |
| `coolify_capabilities` | Instance version and available update, each token's rights and scope and what it sees, the features your setup unlocks and those it does not (with the reason and what would unlock them), the protected environments, warnings |
| `coolify_overview` | One compact snapshot. It opens with "NEEDS YOU", the attention list worst first, each item with its fix. Then servers with current CPU, RAM, disk, load and network; deployments running now; the latest 8 finished or failed deploys of every environment, with trigger and whether a later deploy fixed a failure; every application, service and database with project, environment, status and domain. Filters by project or environment. Cached for 10 seconds |
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

Every action needs the API token's `deploy` right and a target that token can see; the read-only
token is never used for an action, even if it holds `deploy`. Targets are matched by exact name or
id only, never by part of a name. `stop` always sends `docker_cleanup=false`: Coolify's default
would run a Docker cleanup on the whole server.

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

### Protected environments

The environments in `protected_environments` (default `production`) are off limits for the agent:

| Who acts | Switch "Agent may act on protected environments" off (default) | Switch on |
| --- | --- | --- |
| The agent (`coolify_action`) | Refused without a request to Coolify. The answer names the environment and says that you can use the cockpit's button or switch the setting on | Asks every time. The prompt starts with "PROTECTED ENVIRONMENT" and the environment's name, and an "always" or "for this session" answer does not carry over to the next call |
| You (the page's buttons) | Works, with a confirm that names the environment | Same |

An environment the cockpit cannot determine counts as protected. A chat message alone does not lift
the refusal, because the plugin cannot verify who wrote it. Protected environments guard the
cockpit's tool, not the token: anything else that runs as Hermes on the host can still read the
token from the profile.

### Arranging the cockpit

The cockpit is a list of widgets, top to bottom, each with a size (`s`, `m`, `l`) and its own
settings. Start from a preset, change it in the page, or ask Hermes ("put a Deploy desk for staging
at the top with a restart button for my-app-staging"); the page and the agent edit the same layout.

| Widget | Shows | Needs |
| --- | --- | --- |
| `summary` | What needs you: one chip per signal with its count; a click filters the Apps list. One green line ("28 apps healthy, last deploy 12 min ago") when nothing does. Offers "Redeploy N apps with changed env" | `read` |
| `deployments` | Latest deploys of every environment, successes included, newest first: status, app, environment, commit message, trigger, age, duration. A failed row offers **Diagnose** (the card opens in place) and **Roll back** to the last good deploy; a failure that a later success replaced is dimmed | `read` |
| `resources` | Apps of every environment, grouped by environment, with filter chips, deployed commit and its age, signal badges, **Deploy** and **Restart**; services and databases in a second group. A protected environment shows a lock. Apps of one repository in two environments show as a pipeline pair | `read` |
| `servers` | One card per server: CPU, RAM, disk, load and network (in and out), each a value over an area chart, with deploy ticks, hover, peak and a disk estimate; one range switch (1 h, 6 h, 24 h, 7 d) for all servers | a team-wide `read` token (stats: MCP and Sentinel metrics on) |
| `quick_actions` | Pinned buttons, each one action on one resource | the API token's `deploy` |
| `notebook` | Your instance notebook | nothing |
| `chat` | Ask about what you are looking at | nothing |

Presets: **Overview** (the default: summary, latest deploys, apps, servers) and **Deploy desk** (one
environment in focus: its apps with row actions and its latest deploys, optional quick-action
buttons on top). A preset can replace the layout or join it at the top or bottom.

Unknown widgets or settings are refused with the list of known ones. Quick-action buttons name an
action from the same list `coolify_action` uses (`cancel` is not pinnable; it acts on one
deployment); the resource is looked up only when you click, inside the API token's view, and the
click asks for confirmation. Every save carries the layout's version: if the agent changed the
layout while you were editing it (or the other way round), the older save is refused and you get
the current layout back, so neither edit is lost silently.

### The Coolifybrücke page in Hermes Desktop

The plugin's Desktop half adds a **Coolifybrücke** row to the sidebar, a status-bar chip (a health
dot, deployments running, apps failing, an arrow when a Coolify update is out; "Coolify: setup
needed" before the setup is done), the command **Open Coolifybrücke**, and an entry **Coolify
Cockpit** in **Settings → Plugins**. It ships switched off: after installing, turn it on in
**Capabilities → Plugins**.

The page draws your saved layout as a grid of widgets (see the table above). A widget your token
setup cannot fill says in one line why, and what would unlock it.

On a failing app, **Roll back to `abc1234`** returns it to its last good commit. A deploy you
started that fails opens its diagnosis without a click, with **Ask Hermes to fix**. Every action
button opens a confirmation naming the action, the resource, its environment and server, the token
and the request; on a protected environment it adds the line "This is <environment>." Only then is it sent, and the
page follows the deployment until it ends. **Redeploy N apps with changed env** asks once for all of
them and lists each. **Edit layout** lets you add, remove, reorder and resize widgets, change their
settings and apply a preset; a save that collides with a newer version (the agent's) is refused,
and the page reloads the newer layout and tells you.

"Ask" on a server, resource or deployment, and the chat widget, open a normal Hermes chat titled
**Cockpit** as a tab, with your question drafted and an `@coolify:` reference for the item; the
next question reuses that tab. Nothing is sent until you press send; the chat uses your
configured provider and model.

**Settings → Plugins → Coolifybrücke** holds two cards, and the settings form sits under the same
entry: **Connection and token** (the result line, the protected environments, each token's rights and scope, and what
your setup unlocks and what it does not, with the reason) and **Alerts** (see
[Alerts with Desktop closed](#alerts-with-desktop-closed)).

The page reads the snapshot every 60 seconds (every 15 while a deployment runs or an action you
sent is being followed), the layout every 15 seconds (so the agent's edits appear), the server
charts of a longer range every 5 minutes, and the token check and notes every few minutes, only
while Hermes Desktop is open. When the backend has not answered after 2.5 seconds on the first
load, the page says that it is starting.

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
data folder). A newer release is also the `update_available` item of the attention list.

### For the agent: skill, `@coolify:` references, redaction

- A bundled skill, `coolifybruecke:coolifybruecke`, teaches the agent which tool answers which
  question, the attention list as the first answer to "what needs me?", the debug order
  (deployments, then details, logs last), the diagnosis catalogue, how to turn alerts on, and the
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

For the Desktop page, under `/api/plugins/coolifybruecke/`: `GET /setup` (which settings are
filled in, no Coolify request), `GET /capabilities`,
`GET /snapshot` (`?refresh=true` skips the cache; it carries the attention list, the per-app fleet
rows and the latest deploys) and `GET /resources/{uuid}/deployments`
(`?take=N`, up to 50) return the same data as the tools, as JSON. `GET /servers/metrics`
(`?range=1h|6h|24h|7d`) returns the server charts for one range and the disk estimate. `GET /actions`
lists the actions and whether the API token holds `deploy`; `POST /actions` with `{kind, target,
params?, dry_run?}` runs one action for the page's own buttons (`dry_run: true` returns the request
and the confirmation text without sending anything). The page confirms each click first; the route
runs the same rights and view checks as the tool, and never refuses a protected environment. `GET
/layout` returns the layout with the widget types and presets (`?availability=true` adds, per
widget, whether your token setup unlocks it and why not); `PUT /layout` with `{expected_version,
layout}` saves the page's edit and answers 409 with the current layout when it changed in between;
`POST /layout/preset` with `{preset, params?, placement?, expected_version?}` applies a preset. `GET
/notes` (`?max_chars=N`) returns the notebook, newest entries first within the cap; `POST /notes`
with `{text, topic?}` adds your entry. `POST /diagnose` with `{log_text}` (a pasted log) or
`{deployment_uuid}` (fetched only with a `read:sensitive` token; otherwise the answer asks for a
paste) returns the diagnosis card the page shows, with secrets masked. `GET /alerts` reads the alert
job back (on or off, schedule, delivery, last check, open items) from Hermes' cron job list and
`alerts-state.json`, without a Coolify request.

**A changed token is used at once, without a restart.** Chats read the token on every call. For a
profile whose routes run in its plugin host process (`HERMES_PLUGIN_HOST_PROCESS=1`, every
profile but the one the backend launched as), the host's environment is the profile's `.env` as it
was when the host started, so each route request reads the plugin's own secrets
(`COOLIFYBRUECKE_*`) through Hermes' secret-scope builder (`agent.secret_scope.
build_profile_secret_scope`), the same one a gateway turn uses; a token removed from the settings
is gone at once there too. In the backend's own process the request already runs in a fresh
profile scope and the routes use `get_secret`. `GET /setup` says which settings are filled in,
without a Coolify request and without a token value. Route errors that a wrong token could explain say so in a `hint` field.

### What a snapshot costs

One snapshot reads, in parallel: `GET /servers` per token (to tell limited from team-wide),
`/applications`, `/services`, `/databases`, `/deployments` (the running queue), `/projects` and
one `/projects/{uuid}` per project (environment names; reused for 10 minutes unless a resource
sits in an environment they do not name), and through Coolify's MCP endpoint
`get_server_metrics` per server with Sentinel metrics on and two `list_deployments` calls
(`status=finished` and `status=failed`, 100 rows each). An app past the edge of those lists, or a
failing app whose last good deploy is older than them, gets one REST history read (at most 5 per
snapshot), kept until it deploys again. Without MCP the snapshot still answers: it reads each app's
history instead, 10 apps per snapshot and each app at most every 5 minutes, and says so. A served
snapshot then adds the due env checks (at most 6) and branch reads (at most 8).
