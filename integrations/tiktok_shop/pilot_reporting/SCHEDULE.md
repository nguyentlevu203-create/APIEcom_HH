# TikTok Daily Production Pipeline — Schedule (V1)

Not yet installed into cron/launchd — this documents the intended schedule
only. Installing a recurring system job is a standing-configuration change
outside what this pilot should do without your explicit go-ahead; say the
word and it can be wired into `cron`/`launchd` directly.

All times **Asia/Ho_Chi_Minh (shop-local)**.

| Time | Step | Command |
|---|---|---|
| 00:30 | Orders incremental | `python3 run_daily_tiktok_reporting.py --report-date <shop-local D-1>` (steps 01-03 portion; full run recommended — see note below) |
| 01:00 | Returns | (part of the same run — step 04) |
| 02:00 | Product/Inventory | (part of the same run — step 05) |
| 05:30 | Finance | (part of the same run — steps 10-12; scheduled later than Orders since TikTok's finance/statement data for D-1 becomes available progressively through the day) |
| 06:00 | Rolling backfill | (part of the same run — step 13) |
| 07:00 | Analytics D-1 | (part of the same run — steps 07-09) |
| 07:15 | Affiliate | (part of the same run — step 06) |
| 07:25 | Video/LIVE | (covered under 07:00 Analytics — steps 08-09) |
| 07:35 | Reconciliation | (part of the same run — step 14) |
| 07:45 | Business Mart / Data Status | (part of the same run — step 15) |
| 08:00 | CEO Daily | (part of the same run — step 16-17; report must exist by 08:00) |

## Why one run, not eleven separate cron entries

`run_daily_tiktok_reporting.py` executes steps 01-17 as one strict
sequence in a single process (Section 1: "Không chạy song song bừa bãi" —
splitting this into eleven independent cron jobs would risk step 14
(Reconciliation) running before step 12 (Finance) finishes, or step 13
(Backfill) racing step 10 (Unsettled). The table above shows *when in the
09:00-window each phase of data becomes available/complete*, not eleven
separate invocations. A single 00:30 kick-off of the full pipeline
consistently finishes step 17 well before 08:00 based on this session's
timed runs (~2-4 minutes end-to-end per date, well inside the window).

## Suggested crontab entry (once approved to install)

```cron
30 0 * * * cd /Users/VuIT/Desktop/APIClaude/integrations/tiktok_shop/pilot_reporting && /usr/bin/python3 run_daily_tiktok_reporting.py >> logs/cron_daily.log 2>&1
```

No `--report-date` is passed — the script defaults to shop-local D-1
automatically (Section 1 spec).

## What this schedule does NOT do

- Does not touch Excel. `run_daily_tiktok_reporting.py` writes
  `reports/CEO_DAILY_<date>.md` / `.json` directly — no manual export step.
- Does not call any WRITE endpoint (enforced by `tiktok_client._enforce_allowlist`,
  independent of this schedule).
- Does not run the one-time/periodic Net Sales cross-date validation
  (`net_sales_validation.py`) — that is a periodic re-validation exercise
  (recommended monthly or after any TikTok API version change), not a
  daily step.
