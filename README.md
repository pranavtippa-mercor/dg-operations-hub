# Doppelganger Operations

One GitHub Pages application for task completion, staleness triage, and hourly module health. Task views share one normalized roster, filters, links, exports, and detail panel.

## Use

- **Tasks → Completion:** five cohorts, Studio-qualified RFD and Delivered counts, pipeline/domain distribution, deadline confirmations, and shared search/filters.
- **Tasks → Staleness:** stage-specific activity clocks, deadline urgency, flag history, holder/queue summaries, recent actors, and local notes.
- **Module health:** hourly execution failure grid, dimension drilldowns, percentages/fractions, time/module filters, and CSV export.
- **Data & refresh:** independent source dates, snapshot import/export, and refresh mode.

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

The app owns all three collectors. Legacy tracker directories are used only by the explicit one-time migration command. Both local and hosted execution use the same collectors and private state. Local execution uses the authenticated Codex connection. Hosted execution uses a dedicated Mercor API key through Mercor's HTTPS tool gateway, with the same read-only restrictions. Source credentials never reach the website or generated Git branches.

`python3 scripts/collect.py --live` performs one Studio inventory SELECT for completion and activity, refreshes world definitions, and resolves unknown owners. Fixed HTML roster coverage, previous unified IDs, status definitions, source world, and aggregate arithmetic must validate before the published snapshot is replaced. Newly discarded tasks are present in the source query, then removed from displayed denominators.

The Slack collector combines scoped thread reads with incremental task-reference discovery. After its initial baseline, complete channel searches identify changed threads; unchanged verified threads are reused, with a daily full-thread check. Evidence retains the original author, message time and link. Only attributable, explicit dates become deadline commitments; ambiguous replies remain visible evidence. A confirmation classification is a heuristic, not an independently verified promise. Source dates and coverage remain separate from task status.

The module collector queries Datadog directly, joins audit identities to Studio's production world, and builds the execution/dimension aggregates deterministically. Private caches avoid repeating unchanged historical work. No language-model turn or legacy artifact publication is needed to refresh metrics.

`python3 scripts/service.py start` starts one detached coordinator. `Start Operations Refresh.command` does the same; the terminal may be closed. It schedules independent collectors, prevents duplicate publishers and overlapping source jobs, serializes connector calls across workers, retries failed collectors after five minutes, and retains their last successful evidence. `python3 scripts/service.py status` checks the exact updater process; `stop` requests its shutdown. After a reboot or logout, start it again. This Mac must remain awake and signed in with working Codex and GitHub connections. GitHub Pages serves the static app; collection runs locally.

## Hosted refresh

The `Refresh encrypted operations data` workflow runs finite collection and publication jobs on GitHub's Linux runners. Once activated and verified, it needs no Mac, local Codex session, or open browser. The workflow is gated by the repository variable `DG_HUB_HOSTED_ENABLED=true`; manual runs remain available before activation. Local collection stays active until the cloud source connection and first complete hosted refresh have succeeded.

The schedule requests a run every five minutes, offset from the start of the hour. A run refreshes task data and only the Slack/module collectors that are due. Persisted watermarks retain the 15-minute Slack and hourly module intervals. GitHub may delay or drop scheduled runs; these intervals are targets, not timing guarantees. Public-repository schedules can be disabled after 60 days without repository activity. See [GitHub's scheduled workflow documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule). Standard public-repository runners have [no Actions compute charge](https://docs.github.com/en/billing/concepts/product-billing/github-actions).

Hosted configuration uses three Actions secrets:

| Secret | Purpose |
|---|---|
| `MERCOR_API_KEY` | Dedicated source connection, restricted to the five tools below |
| `DG_HUB_ACCESS_KEY` | Existing dashboard reader key, so the app's unlock key stays the same |
| `DG_HUB_STATE_KEY` | Separate 32-byte base64url key protecting complete collector caches |

Create the source key in [Team Platform API Keys](https://team.mercor.com/settings/integrations/api-keys), using a purpose name such as `coil-dg-operations` and these exact allowed tools:

```text
studio
slack_read_thread
slack_search_public_and_private
datadog_load_datadog_skill
datadog_analyze_datadog_logs
```

Save it directly as `MERCOR_API_KEY` in [repository Actions secrets](https://github.com/pranavtippa-mercor/dg-operations-hub/settings/secrets/actions). Never paste credentials into chat, source code, command arguments, or logs. The gateway uses `POST /tools/<tool_name>` with raw tool-schema JSON, as documented in Mercor's [API-key guide](https://github.com/Mercor-io/mercor-skills/blob/main/plugins/mercor-skills-team-agents/skills/mercor-coil-api-keys/SKILL.md). The `studio` proxy is additionally restricted by this application to GET requests and a single SELECT query. There are no Slack sends or Studio mutations. Source-key creation requires the account owner's UI access; Codex can configure the other secrets, migrate state, run verification, and activate the schedule programmatically under the user's authorization.

The generated `collector-state` branch stores an encrypted manifest and encrypted file chunks. Unchanged chunks reuse existing Git blobs, avoiding repeated uploads of full Slack histories. The state key is separate from the dashboard key because collector caches include complete source threads. Only an explicit file allowlist is transferred; source credentials, Ryu notes, logs, locks, and process files are excluded. Restores validate authenticated ciphertext, file hashes, sizes, and paths. Plaintext exists only in the runner's private working directory and is removed at the end of the job. No plaintext Actions caches or artifacts are uploaded.

The workflow runs only trusted `main` code, pins its setup actions, serializes runs, and uses GitHub's job-scoped token to publish within this repository. Successful source updates can publish even if another source fails; failed sources retain their previous freshness dates, and the workflow reports failure. State progress is saved after both successful and partial runs.

For cutover, initialize the encrypted state, manually run the workflow with `refresh_all=true`, and verify all source dates plus the candidate snapshot's browser-compatible decryption. While `DG_HUB_HOSTED_ENABLED` is false, a manual run saves its candidate state without publishing to the live dashboard. A `verify_state_only=true` run can separately test Linux restoration and encryption without source credentials. After complete source verification, stop the local publisher and wait for its exit, enable `DG_HUB_HOSTED_ENABLED`, and dispatch a full hosted publication. Mark `.private/hosted-active.json` with `{"active":true}` after successful cutover so old desktop launchers cannot start a duplicate local publisher. These steps can be performed programmatically by Codex. If source authentication later expires or is revoked, replace the Actions secret and run a complete verification again; source-key lifetime is not assumed.

For a deliberate full source verification, `python3 scripts/collector_schedule.py` refreshes the Slack and module sources once. `python3 scripts/publish.py --refresh-all --push` refreshes all sources and publishes, and must run while the resident coordinator is stopped. Routine source failures keep their original freshness timestamps. Collection never sends Slack messages or changes Studio records.

Data refreshes replace the generated `dashboard-data` branch tip without rebuilding the app. That branch contains only the current encrypted snapshot and has no retained history; main source history is preserved. The publisher refuses to replace a branch with unrelated content. The browser checks for published updates every five minutes while visible. It rejects source-date regression and separates imported/published modes; manual import disables feed polling until reconnected.

## Local setup

Requires Node 22+, Python 3.9+ with zoneinfo, macOS Keychain, GitHub CLI authentication, and the existing local Codex/Mercor bridge in `Documents/Ryu/Tools` (override `bridge_dir` in private configuration if needed).

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

`python3 scripts/publish.py` prepares an encrypted snapshot. Add `--push` after the repository exists. The Pages build needs no source credentials or decryption keys. The separate data-refresh workflow uses the three secrets described above.

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
