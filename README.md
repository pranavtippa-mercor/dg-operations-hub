# Doppelganger Operations

One GitHub Pages application for task completion, staleness triage, and hourly module health. Task views share one normalized roster, filters, links, exports, and detail panel.

## Use

- **Tasks → Completion:** five cohorts, Studio-qualified RFD and Delivered counts, pipeline/domain distribution, deadline confirmations, and shared search/filters.
- **Tasks → Staleness:** stage-specific activity clocks, deadline urgency, flag history, holder/queue summaries, recent actors, and local notes.
- **Module health:** hourly execution failure grid, dimension drilldowns, percentages/fractions, time/module filters, and CSV export.
- **Data & refresh:** independent source dates, snapshot import/export, and refresh mode.

Studio links open the authoritative record. Local notes and follow-up drafts never send messages or mutate Studio. A page refresh cannot certify client acceptance.

## Data protection

The public repository and site contain application code. A separate generated `dashboard-data` branch contains a **gzip-compressed, AES-256-GCM encrypted snapshot**. The encryption key is generated randomly, stored in macOS Keychain (`dg-operations-hub`), and never committed. PBKDF2-SHA256 uses 600,000 iterations with a fresh random salt; every snapshot has a fresh GCM nonce.

Open `Open Operations Hub.command` to open the deployed site with the local access key in the URL fragment. The page removes the fragment immediately. The fragment is not sent to GitHub. Anyone with the access key can decrypt the published snapshot; share it only with intended readers. `Copy Access Key.command` copies it to the clipboard when needed. Lock clears the in-memory snapshot/key and invalidates outstanding requests. Device-local follow-up notes remain until explicitly cleared in Data & refresh.

The unencrypted snapshot, configuration and scan history live in ignored `.private/`. `scripts/check_public.py` checks static output for task IDs, source world IDs and credential patterns before deployment.

## Source flow

```text
One read-only Studio inventory + current world definitions
    + cached Slack confirmations / activity / deadline evidence
    + existing hourly module aggregates
                 ↓
Validated normalized snapshot → encrypted data → GitHub Pages
                 ↓
Both task views and module health share one client state
```

`python3 scripts/collect.py --live` uses the existing authorized local Codex/Mercor bridge. One SELECT supplies completion and activity fields, with current world definitions and owner lookup when necessary. It does not modify the old trackers. Fixed HTML roster coverage, previous unified IDs, status definitions, source world, and aggregate arithmetic must validate before the last good snapshot is replaced. Newly discarded tasks are present in the source query, then removed from displayed denominators. New source IDs cannot silently vanish on a later partial read.

The module data is the existing validated `module-failure-dashboard/data.json`, not its incomplete scratch TSVs. Its hourly source process remains necessary. Slack confirmations use `confirmations.json` directly; harvesting remains separate and the original dates stay visible. Confirmation classifications are inherited heuristic evidence, not verified promises.

`python3 scripts/publish.py --watch --push` refreshes task data and publishes compressed encrypted snapshots every five minutes while this Mac is awake and the process is running. `Start Operations Refresh.command` starts that resident process; Ctrl-C stops it. It does not create cron, launchd, a broad research sweep, or cloud backups. Module refresh cadence remains one hour from its existing source. GitHub Pages itself only serves static files. Data refreshes replace the generated `dashboard-data` branch tip without rebuilding the app. That branch contains only the current encrypted snapshot and has no retained history; main source history is preserved. The publisher refuses to replace a branch with unrelated content. The browser checks for published updates every five minutes while visible; it does not talk directly to Studio or Datadog.

A failed refresh retains the last published snapshot. The client rejects source-date regression, separates imported/published modes, and shows stale-source warnings. A manual import disables feed polling until reconnected. Cached imports label activity timing unknown because old renderer timestamps do not establish Studio read times.

## Local setup

Requires Node 22+, Python 3.9+ with zoneinfo, macOS Keychain for publishing, GitHub CLI authentication, and the existing local source adapters.

Create `.private/config.json` (never commit it):

```json
{
  "completion": "/path/to/html-ee-tracker/artifact",
  "staleness": "/path/to/dg-staleness",
  "modules": "/path/to/module-failure-dashboard/data.json"
}
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

After GitHub authentication, create/link the repository, push `main`, and configure Pages to deploy through GitHub Actions. The included workflow tests and builds the static site, checks public output, then deploys it. App URL: `https://pranavtippa-mercor.github.io/dg-operations-hub/` (only live after successful publication).

`python3 scripts/publish.py` prepares an encrypted snapshot. Add `--push` after the repository exists. No source credentials or decryption keys are needed in GitHub Actions.

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
