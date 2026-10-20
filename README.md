# Reinsurance Federal Updates Tracker

## Google Sheets setup

In Settings > Secrets and variables > Actions, set `GOOGLE_CREDENTIALS_JSON` to the **entire original service-account JSON file**, pasted as raw JSON (recommended). Legacy base64-encoded UTF-8 JSON is also accepted. Do not paste only a private key, a file path, an OAuth client configuration, or escaped/quoted JSON. Do not commit credentials or put their values in logs, issues, or pull requests.

Set `SHEETS_SPREADSHEET_ID` to the identifier between `/d/` and `/edit` in the Sheet URL. Enable the Google Sheets API in the service account's Google Cloud project, share the destination spreadsheet with the JSON file's `client_email` as **Editor**, and ensure it has a worksheet named **Federal Updates**. The production workflow explicitly enables Sheets export; the old `SHEETS_ENABLED` secret is no longer used by it.

Existing columns A:G are preserved: collected time, source, title, URL, keywords, publication date, notes. URLs in column D identify rows already delivered. Existing duplicates are not deleted, and rows sharing a URL are treated as the same item. Values are appended as literal text, preserving notes and avoiding interpretation as formulas.

## State, failures, and recovery

The workflow serializes production executions and restores the latest unexpired database artifact from this workflow on the default branch, including runs whose export failed. It validates the database before use and uploads it even after an export failure. It fails rather than silently starting over if no valid state is available. Artifacts are retained for 90 days: keep an independent backup, especially before a long pause.

Legacy databases are migrated in place. Existing rows become pending exports; collection deduplication and export confirmation are separate. Pending exports retry even when collection finds no new items. Each confirmed batch is marked delivered. A retry checks Sheet URLs first, so an API timeout after a write or a lost database checkpoint does not normally create duplicate rows. External simultaneous writers or edited/removed URL cells can defeat this reconciliation.

For recovery after merging this correction:

1. Correct the credentials/settings above.
2. Open Actions > Reinsurance Federal Updates Tracker. If disabled, click **Enable workflow** (or the inactivity acknowledgement button). A pull request alone does not activate changes on the default branch.
3. Choose **Run workflow**, use the default branch and select **export_only**. This restores the latest saved database and exports pending rows without fetching sources or sending email/Slack notifications.
4. Verify the run's export count and the destination worksheet. A failed export now fails the run and keeps unconfirmed rows pending.

The October 17, 2026 artifact is ID **12099694168**, run **39030017187**. Its archive SHA256 is `13123ee43ca830fdcc3e4160049699b4a57ee4c993583f7e35f1b51c86ae9d62`. Verified locally: database integrity OK, **199 rows**, all with URLs. The first corrected run will restore this artifact if no newer production state has been uploaded. It can recover these rows even after they leave the source's seven-day search window. If a newer artifact has superseded it, preserve that newer state and merge the old rows by ID into a backup before export; do not overwrite newer state blindly.

For local recovery in a secured environment: download the artifact, run `python restore_state.py artifact.zip`, provide the same environment settings (including `SHEETS_ENABLED=true`), then run `python reinsurance_tracker.py --export-only`. Do not commit the database or credentials.

## Schedule

Investigation on October 20 found workflow state `disabled_inactivity`, with October 17 the latest scheduled run and August 17 the latest repository commit. GitHub disables scheduled workflows in public repositories after 60 days without repository activity. See [GitHub's enable/disable instructions](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows). Re-enable after the fix is merged and check periodically; successful Actions runs alone do not prevent inactivity disabling. No artificial keepalive commits or expanded write permissions are used.

The schedule is daily at **08:17 UTC** (04:17 Eastern daylight time / 03:17 Eastern standard time), moved away from the top of the hour to reduce queue contention. The old 08:00 schedule was also UTC, not 8 a.m. Eastern. Scheduled runs can be delayed; the 60-day inactivity disabling was the documented cause of the current stop.

## Validation

Install with `python -m pip install -r requirements.txt`; run `python -m unittest discover -s tests -v`. CI runs tests without production credentials. Tests cover credential formats and sanitized errors, failed/partial export retries, existing Sheet URLs, legacy migration, disabled exports, no-new-item retries and artifact validation. Live Google authentication/export requires the repository secret and Sheet access; a passing unit test does not confirm delivery.
