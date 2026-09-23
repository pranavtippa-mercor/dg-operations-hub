# Doppelganger Operations

One GitHub Pages application for task completion, staleness triage, and hourly module health. Task views share one normalized roster, filters, links, exports, and detail panel.

## Use

- **Tasks → Completion:** five cohorts, Studio-qualified RFD and Delivered counts, pipeline/domain distribution, deadline confirmations, and shared search/filters.
- **Tasks → Staleness:** stage-specific activity clocks, deadline urgency, flag history, holder/queue summaries, recent actors, and local notes.
- **Module health:** hourly execution failure grid, dimension drilldowns, percentages/fractions, time/module filters, and CSV export.
- **Data & refresh:** independent source dates, snapshot import/export, and refresh mode.

Click a task or module column heading to sort, then click again to reverse it. Each field in a combined task heading sorts separately. Arrows show the active direction; missing dates and unobserved module rates stay last. Task sorting applies before pagination and also sets CSV order. Module rates use the selected time window or hour, with dimensions kept under their module.

Studio links open the authoritative record. Local notes and follow-up drafts never send messages or mutate Studio. A page refresh cannot certify client acceptance.

## Data protection

The public repository and site contain application code. A separate generated `dashboard-data` branch contains a **gzip-compressed, AES-256-GCM encrypted snapshot**. Its random access key is stored in macOS Keychain (`dg-operations-hub`) and the GitHub Actions secret `DG_HUB_ACCESS_KEY`, never in Git. PBKDF2-SHA256 uses 600,000 iterations with a fresh random salt; every snapshot has a fresh GCM nonce.

Open `Open Operations Hub.command` to open the deployed site with the local access key in the URL fragment. The page removes the fragment immediately. The fragment is not sent to GitHub. Anyone with the access key can decrypt the published snapshot; share it only with intended readers. `Copy Access Key.command` copies it to the clipboard when needed. Lock clears the in-memory snapshot/key and invalidates outstanding requests. Device-local follow-up notes remain until explicitly cleared in Data & refresh.

The unencrypted snapshot, configuration and scan history live in ignored `.private/`. `scripts/check_public.py` checks static output for task IDs, source world IDs and credential patterns before deployment.

## Source flow

```text
Studio inventory + current world definitions (5 minutes)
Slack task threads, confirmations and explicit commitments (15 minutes)
Datadog execution/verdict logs + Studio audit/world mapping (1 hour)
                 ↓
Private source caches → validated snapshot → encrypted data → GitHub Pages
                 ↓
Both task views and module health share one client state
```

The app owns all three collectors. Legacy tracker directories are used only by the explicit one-time migration command. Both local and hosted execution use the same collectors and private state. Local execution uses the authenticated Codex connection. Hosted execution reads Studio directly with a dedicated campaign-scoped read-only Studio key; Slack and Datadog use a separate Mercor gateway key. Source credentials never reach the website or generated Git branches.

`python3 scripts/collect.py --live` performs one Studio inventory SELECT for completion and activity, refreshes world definitions, and resolves unknown owners. Fixed HTML roster coverage, previous unified IDs, status definitions, source world, and aggregate arithmetic must validate before the published snapshot is replaced. Newly discarded tasks are present in the source query, then removed from displayed denominators.

The Slack collector combines scoped thread reads with incremental task-reference discovery. After its initial baseline, complete channel searches identify changed threads; unchanged verified threads are reused, with a daily full-thread check. Evidence retains the original author, message time and link. Only attributable, explicit dates become deadline commitments; ambiguous replies remain visible evidence. A confirmation classification is a heuristic, not an independently verified promise. Source dates and coverage remain separate from task status.

The module collector queries Datadog directly, joins audit identities to Studio's production world, and builds the execution/dimension aggregates deterministically. Private caches avoid repeating unchanged historical work. No language-model turn or legacy artifact publication is needed to refresh metrics.

GitHub Actions runs the unified coordinator and publishes encrypted data independently of this Mac. The local publisher is stopped. `python3 scripts/service.py status` checks its process state; `Start Operations Refresh.command` respects the hosted-active marker and leaves collection on GitHub. Source jobs retain their last successful evidence after failures.

## Hosted refresh

The `Refresh encrypted operations data` workflow runs finite collection and publication jobs on GitHub's Linux runners. Hosted execution is active with `DG_HUB_HOSTED_ENABLED=true` and needs no awake Mac, local Codex session, or open browser. The dashboard URL and reader key are unchanged.

**Activation verified:** a complete hosted run refreshed Studio, Slack, and module data; the published encrypted feed was downloaded, decrypted, and matched to the verified hosted snapshot. Native Studio reads use `STUDIO_API_KEY`; scoped Slack and Datadog reads use the Mercor gateway key. The local publisher is stopped and its launcher guard is active.

The schedule requests a run every five minutes, offset from the start of the hour. A run refreshes task data and only the Slack/module collectors that are due. Persisted watermarks retain the 15-minute Slack and hourly module intervals. GitHub may delay or drop scheduled runs; these intervals are targets, not timing guarantees. Public-repository schedules can be disabled after 60 days without repository activity. See [GitHub's scheduled workflow documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule). Standard public-repository runners have [no Actions compute charge](https://docs.github.com/en/billing/concepts/product-billing/github-actions).

The active hosted configuration uses four Actions secrets. The setup details below also apply when a source credential needs replacement:

| Secret | Purpose |
|---|---|
| `MERCOR_API_KEY` (currently supplied by `UNIFIED_091826`) | Slack and Datadog connection, restricted to the four tools below |
| `STUDIO_API_KEY` | Native Studio key, restricted to **Project Doppelganger** and **Read-only** |
| `DG_HUB_ACCESS_KEY` | Existing dashboard reader key, so the app's unlock key stays the same |
| `DG_HUB_STATE_KEY` | Separate 32-byte base64url key protecting complete collector caches |

To provision or replace the Mercor gateway connection, create its key in [Team Platform API Keys](https://team.mercor.com/settings/integrations/api-keys), using a purpose name such as `coil-dg-operations` and these exact allowed tools:

```text
slack_read_thread
slack_search_public_and_private
datadog_load_datadog_skill
datadog_analyze_datadog_logs
```

Save it directly as `MERCOR_API_KEY` in [repository Actions secrets](https://github.com/pranavtippa-mercor/dg-operations-hub/settings/secrets/actions). The existing `UNIFIED_091826` secret is also accepted. The gateway uses `POST /tools/<tool_name>` with raw tool-schema JSON, as documented in Mercor's [API-key guide](https://github.com/Mercor-io/mercor-skills/blob/main/plugins/mercor-skills-team-agents/skills/mercor-coil-api-keys/SKILL.md). Enabling its `studio` tool does not supply Okta identity; hosted Studio reads use the separate native connection below.

To provision or replace the native Studio connection, create its key at [Studio API Keys](https://studio.mercor.com/admin/api): **Create API Key → Specific projects → Project Doppelganger → Read-only**. Name it `dg-operations-hub-readonly` and choose an expiry; a scoped key may use **Never**. Save the generated value directly as `STUDIO_API_KEY` in the repository's Actions secrets. The current Studio proxy restricts key-management endpoints, so key creation requires this UI. Codex can verify the configured connection and refresh the hosted data programmatically after the secret is saved. [Studio's official setup guide](https://github.com/Mercor-io/mercor-skills/blob/main/plugins/mercor-skills-product-studio/skills/studio-api/references/setup.md) documents the native key and production API.

Never paste credentials into chat, source code, command arguments, or logs. `scripts/remote_studio.py` sends the native key only to `https://api.studio.mercor.com`, refuses redirects, and permits only the collectors' GET endpoints and a single SELECT query. Source headers are limited to the configured campaign/company/account. There are no Slack sends or Studio mutations.

The generated `collector-state` branch stores an encrypted manifest and encrypted file chunks. Unchanged chunks reuse existing Git blobs, avoiding repeated uploads of full Slack histories. The state key is separate from the dashboard key because collector caches include complete source threads. Only an explicit file allowlist is transferred; source credentials, Ryu notes, logs, locks, and process files are excluded. Restores validate authenticated ciphertext, file hashes, sizes, and paths. Plaintext exists only in the runner's private working directory and is removed at the end of the job. No plaintext Actions caches or artifacts are uploaded.

The workflow runs only trusted `main` code, pins its setup actions, serializes runs, and uses GitHub's job-scoped token to publish within this repository. Successful source updates can publish even if another source fails; failed sources retain their previous freshness dates, and the workflow reports failure. State progress is saved after both successful and partial runs.

Collector-state metadata reads and saves retry temporary server/network failures up to four total attempts, with a 60-second request timeout and a 120-second total wait budget per request. Rate-limit retries honor GitHub's cooldown; longer cooldowns stop the attempt instead of retrying too early. Authentication, permission and validation errors fail immediately. Reference-write retries first check the branch: an already-applied write counts as success, while a changed branch is preserved. Public diagnostics report only fixed operation names, HTTP status, error category and retry timing, never raw responses or private content. Saving collector state happens after dashboard publication, so a failed state save can mark the workflow red even when the dashboard update succeeded.

Failure diagnostics also include bounded request byte/entry counts, a validated GitHub request ID, and allowlisted error categories. A terminal GitHub HTTP error retains up to 64 KiB of its response after credential redaction, encrypted with the separate collector-state key and a dedicated cryptographic purpose. Only that ciphertext is uploaded as an Actions artifact, with one-day retention. Request contents, credentials, arbitrary headers and raw subprocess output are excluded. This diagnostic survives a failed collector-state save without publishing private details in logs.

The optional `probe_studio_only=true` manual run performs one existing Studio inventory read. Public logs contain bounded response-structure metadata only. A bounded, credential-redacted diagnostic is retained in encrypted collector state for private troubleshooting; it is never published as plaintext.

For a complete source check or an immediate refresh, manually run the GitHub workflow with `refresh_all=true`. It uses the same serialized publisher as scheduled runs. With the hosted gate enabled, the run publishes the refreshed encrypted snapshot. Verify the workflow result and the separate source dates in Data & refresh. Codex can dispatch and verify this refresh programmatically. A `verify_state_only=true` run separately checks Linux state restoration and decryption without source calls or publication.

The local `.private/hosted-active.json` marker prevents desktop launchers from restarting a second collector. A deliberate return to local collection requires disabling `DG_HUB_HOSTED_ENABLED`, waiting for active or queued hosted publication runs to finish, and then clearing the marker before starting the local service. Codex can perform and verify that coordinated change programmatically when requested. Local collection then requires the Mac to remain awake and signed in.

If a source credential expires or is revoked, replace the corresponding Actions secret using the source setup above, then run a complete hosted verification. Credential lifetime is determined by the source provider.

Data refreshes replace the generated `dashboard-data` branch tip without rebuilding the app. That branch contains only the current encrypted snapshot and has no retained history; main source history is preserved. The publisher refuses to replace a branch with unrelated content. The browser checks for published updates every five minutes while visible. It rejects source-date regression and separates imported/published modes; manual import disables feed polling until reconnected.

## Local development and recovery

Local development or recovery requires Node 22+, Python 3.9+ with zoneinfo, macOS Keychain, GitHub CLI authentication, and the existing local Codex/Mercor bridge in `Documents/Ryu/Tools` (override `bridge_dir` in private configuration if needed).

Run `scripts/migrate_sources.py` once with the three existing tracker directories. It imports roster metadata, historical evidence and bootstrap caches into `.private/`, without replacing existing unified state. Review its `.private/config-v2.json`, then activate it as `.private/config.json`. Never commit either file. Version 2 configuration contains only source scope, private cache paths and collector intervals; it has no recurring dependency on legacy tracker paths.

```sh
python3 scripts/migrate_sources.py \
  --completion /path/to/html-ee-tracker/artifact \
  --staleness /path/to/dg-staleness \
  --modules /path/to/module-failure-dashboard
```

```sh
npm ci
npm run collect
npm test
npm run typecheck
npm run lint
GITHUB_PAGES=true npm run build:pages
python3 scripts/check_public.py
npm run preview
```

The local preview at `http://127.0.0.1:5189/dg-operations-hub/` loads the private snapshot from a loopback-only endpoint. Public output never includes that endpoint or raw snapshot. The shell supports JSON file import if no published encrypted feed is available.

## Deploy

Target owner: `pranavtippa-mercor`; repository: `dg-operations-hub`.

The repository is linked at https://github.com/pranavtippa-mercor/dg-operations-hub, with Pages configured to deploy through GitHub Actions. Push source changes to `main`. The included workflow tests and builds the static site, checks public output, then deploys it. App URL: `https://pranavtippa-mercor.github.io/dg-operations-hub/` (served by GitHub Pages).

The Pages build needs no source credentials or decryption keys. Data publication is handled by the separate hosted refresh workflow using the four secrets described above; updating source code and refreshing dashboard data are separate operations.

## Definitions preserved and corrected

- Native `rfd_due` wins over old commitments. A bare date means 23:59 America/Los_Angeles, including daylight saving time. Removed Studio dates are not resurrected from cached assignments.
- Generic `updated_at`, stage transition, Slack activity, and confirmations are separate clocks. An update is not proof of meaningful contributor work.
- Thresholds: writing 12h; runs 6h; review/audit/edits 4h; queues 2h. Half-threshold with update and transition less than 72 seconds apart means “No update since claim.” Twice-threshold suggests reviewing reassignment; it never reassigns automatically.
- Red zone means stale plus overdue or under 25% of the stage-to-deadline window remaining. Slack activity never clears Studio activity flags.
- RFD Audit Passed is ready inventory but only counts as Studio-qualified RFD if the live status definition says so. Delivered and Frozen remain distinct.
- Module rate = failed / finished executions. Dimension rate = fail / (fail + pass), excluding neutral. Totals sum numerators and denominators. No observations render as “—”.
- Hour buckets use UTC instants displayed in Pacific time so repeated DST hours remain distinct. Current hour can be partial.

## Verification

Meaningful tests cover stage thresholds, queue/blocked/terminal precedence, deadline/DST semantics, malformed input rejection, denominator arithmetic, CSV formula safety, and Node-to-browser encryption compatibility/tamper rejection. Browser visual/interaction testing requires an available browser session; it was unavailable in the build environment.
