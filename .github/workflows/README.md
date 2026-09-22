# SAM.gov Contract Opportunity Monitor

Checks SAM.gov daily for new federal contract opportunities matching your keywords and NAICS codes, remembers what it has already reported, and sends only genuinely new matches to Slack or email.

**Runs entirely on GitHub's servers — nothing needs to be installed on your computer.**

---

## Why GitHub Actions

| | GitHub Actions | Running locally |
|---|---|---|
| Python install required | No | Yes |
| Runs when your laptop is off | Yes | No |
| Secrets storage | Encrypted repo secrets | Plain text in a file |
| Cost | Free for public repos | Free |
| Run history | Kept in the Actions tab | Local log file |

Public repositories get unlimited free Actions minutes. A private repo gets 2,000 minutes per month, and this job uses roughly one minute per day.

---

## Setup (about 10 minutes, all in a browser)

### 1. Get a SAM.gov API key

1. Sign in at [sam.gov](https://sam.gov) (free account)
2. Click your profile icon, then **Account Details**
3. Re-enter your password to reveal the **Public API Key**
4. Copy it somewhere handy for step 4

### 2. Create a repository

1. Go to [github.com/new](https://github.com/new)
2. Name it something like `sam-monitor`
3. Choose **Private** if you would rather keep your keyword list to yourself
4. Click **Create repository**

### 3. Upload the project files

On the new repository page, click **uploading an existing file**, then drag in everything from this folder. Two notes:

- Keep the folder structure intact. The workflow must end up at `.github/workflows/sam-monitor.yml`
- If the drag-and-drop skips the `.github` folder (browsers sometimes hide dotted folders), use **Add file → Create new file**, type `.github/workflows/sam-monitor.yml` as the filename, and paste the contents in directly

Commit the upload.

### 4. Add your secrets

In the repository, go to **Settings → Secrets and variables → Actions**, then **New repository secret**.

Add this one first:

| Secret name | Value |
|---|---|
| `SAM_API_KEY` | The key from step 1 |

Then add secrets for whichever alert channel you want.

**For Slack:**

| Secret name | Value |
|---|---|
| `SLACK_WEBHOOK_URL` | Your incoming webhook URL |

Get the webhook at [api.slack.com/apps](https://api.slack.com/apps): **Create New App → From scratch**, name it, pick your workspace, then **Incoming Webhooks → Activate → Add New Webhook to Workspace**, choose a channel, and copy the URL.

**For email:**

| Secret name | Example |
|---|---|
| `EMAIL_SMTP_HOST` | `smtp.gmail.com` |
| `EMAIL_SMTP_PORT` | `587` |
| `EMAIL_USERNAME` | `you@gmail.com` |
| `EMAIL_PASSWORD` | A Gmail [App Password](https://myaccount.google.com/apppasswords), not your normal password |
| `EMAIL_FROM` | `you@gmail.com` |
| `EMAIL_TO` | `you@swri.org` (comma-separate several addresses) |

### 5. Turn your channel on

Edit `.github/workflows/sam-monitor.yml` in GitHub's web editor and flip the matching flag from `'false'` to `'true'`:

```yaml
          SLACK_ENABLED: 'true'     # if using Slack
          EMAIL_ENABLED: 'true'     # if using email
```

These are plain values rather than secrets so you can see at a glance which channels are live.

### 6. Set your search terms

Edit `config.yaml` in the web editor:

```yaml
filters:
  keywords:
    - "systems engineering"
    - "test and evaluation"
  naics_codes:
    - "541330"   # Engineering Services
    - "541715"   # R&D in Physical, Engineering, and Life Sciences
```

Keywords match against the notice **title**. NAICS codes match the assigned code. You can also set these through the `SAM_KEYWORDS` and `SAM_NAICS_CODES` secrets if you prefer not to commit them.

### 7. Test it

Go to the **Actions** tab, select **SAM.gov Monitor**, then **Run workflow**. Set the dry-run option to `true` for the first attempt — it will search and report what it found without sending alerts or saving anything.

Open the run and check the summary panel for a match count. If it looks right, run it again with dry-run set to `false` to receive a real alert.

From then on it runs by itself every morning.

---

## How deduplication works

Every SAM.gov notice carries a unique `noticeId`. After alerting on one, the script records that ID in `history.json` and the workflow commits the file back to your repository. The next run loads it and skips anything already present.

This matters because Actions runners are wiped clean after each job — committing the file is what gives the monitor a memory. A useful side effect is that your commit history becomes a dated log of every opportunity ever found.

Deduplication happens at two levels:

- **Within a run** — the same notice often matches several of your keywords and NAICS codes at once, and is only reported once
- **Across runs** — anything in `history.json` is never reported again

The script also records the date of each run. If a run fails or is skipped, the next one automatically widens its search window to cover the gap instead of losing those days.

---

## Schedule

The default cron is `0 12 * * *`, which is 07:00 Central during daylight saving time.

GitHub cron is always UTC and does not follow daylight saving, so this shifts to 06:00 Central in the winter. To keep it at 07:00 year-round, change the cron to `0 13 * * *` in November and back in March, or simply accept the one-hour drift.

```yaml
on:
  schedule:
    - cron: '0 12 * * *'
```

GitHub also queues scheduled jobs during busy periods, so runs can start a few minutes late. That does not affect results, since the search window is date-based.

---

## Running locally (optional)

If Python does get installed on your machine later, the same code runs locally with no changes:

```bash
pip install -r requirements.txt
python main.py --dry-run --run-once     # test without sending
python main.py --run-once               # single run
python main.py                          # stay running, fire daily at config run_time
python main.py --stats                  # show what is in the history file
```

For local use you may prefer the SQLite backend, which handles very large histories better:

```yaml
storage:
  backend: "sqlite"
```

Keep `backend: "json"` for GitHub Actions, since a binary database is not something git can merge sensibly.

---

## Verifying it without touching the real thing

```bash
python test_local.py
```

This runs 44 offline checks covering both history backends, duplicate suppression, gap recovery, environment overrides, and notification rendering. It makes no network calls and sends no alerts.

---

## Configuration reference

| Setting | Env override | Purpose |
|---|---|---|
| `sam.api_key` | `SAM_API_KEY` | SAM.gov public API key |
| `sam.lookback_days` | `SAM_LOOKBACK_DAYS` | Days to search on the very first run |
| `sam.page_size` | — | Records per API page, max 1000 |
| `sam.active_only` | — | Only return still-open opportunities |
| `filters.keywords` | `SAM_KEYWORDS` | Title keywords, one query each |
| `filters.naics_codes` | `SAM_NAICS_CODES` | NAICS codes to match |
| `filters.set_asides` | — | Set-aside filter; API accepts one per query |
| `filters.state` | `SAM_STATE` | Place-of-performance state, e.g. `TX` |
| `filters.procurement_types` | — | Notice types (`o`, `p`, `r`, `k`, `i`, `a`) |
| `slack.enabled` | `SLACK_ENABLED` | Turn Slack delivery on |
| `slack.webhook_url` | `SLACK_WEBHOOK_URL` | Incoming webhook URL |
| `slack.max_per_message` | — | Opportunities per Slack message |
| `email.enabled` | `EMAIL_ENABLED` | Turn email delivery on |
| `email.smtp_host` | `EMAIL_SMTP_HOST` | SMTP server |
| `email.smtp_port` | `EMAIL_SMTP_PORT` | SMTP port |
| `email.username` | `EMAIL_USERNAME` | SMTP login |
| `email.password` | `EMAIL_PASSWORD` | SMTP password or app password |
| `email.to_addresses` | `EMAIL_TO` | Recipients |
| `storage.backend` | `STORAGE_BACKEND` | `json` or `sqlite` |
| `storage.retention_days` | — | Prune records older than this; `0` never prunes |
| `logging.level` | `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

Environment variables always win over `config.yaml`, which is how secrets stay out of the repository.

---

## Troubleshooting

**The workflow does not run on schedule.** GitHub disables scheduled workflows in repositories with no activity for 60 days. Push any commit to re-enable. Also confirm the file sits at `.github/workflows/sam-monitor.yml`.

**"SAM.gov rejected the API key."** The key is wrong, expired, or the secret name is misspelled. Regenerate it in your SAM.gov Account Details and update the `SAM_API_KEY` secret. Keys can go stale if the account is inactive for a long stretch.

**"Permission denied" on the commit step.** The workflow needs `permissions: contents: write`, which is already in the file. If it still fails, check **Settings → Actions → General → Workflow permissions** and select **Read and write permissions**.

**No matches ever found.** Keywords match titles only, so broad single words work better than long phrases. Try a manual dry run with `LOG_LEVEL` set to `DEBUG`, and sanity-check the same terms in the [SAM.gov search page](https://sam.gov/search/?index=opp).

**Too many matches on the first run.** Lower `sam.lookback_days` to `1` before the first real run, or run once in dry-run mode to gauge the volume first.

**Slack posts nothing but the run is green.** Confirm `SLACK_ENABLED` is `'true'` in the workflow, and that the webhook still exists — Slack revokes webhooks when the app is uninstalled.

---

## Project layout

```
sam_monitor/
├── .github/workflows/sam-monitor.yml   # daily schedule + manual trigger
├── main.py              # entry point, CLI, scheduler, CI reporting
├── monitor.py           # orchestration: window, dedup, dispatch
├── sam_api.py           # SAM.gov API v2 client with retry and paging
├── history.py           # JSON and SQLite dedup backends
├── notifier.py          # Slack Block Kit and HTML email builders
├── config_loader.py     # YAML loading, env overrides, validation
├── config.yaml          # your settings
├── test_local.py        # 44 offline self-checks
├── requirements.txt     # requests, PyYAML, schedule
├── .gitignore
└── history.json         # created on first run, committed by the workflow
```

---

## API notes

- The [SAM.gov Opportunities API](https://open.gsa.gov/api/get-opportunities-public-api/) is free but requires a registered account
- `postedFrom` and `postedTo` are mandatory and cannot span more than one year; the script clamps its window automatically
- There is no OR-style multi-term search, so each keyword and NAICS code is a separate request
- The API has a misspelled response field, `reponseDeadLine`. That is upstream, not a typo in this project
- NAICS codes must be six digits or fewer
