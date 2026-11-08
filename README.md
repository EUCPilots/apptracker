# App Tracker

An application version tracker that uses [Evergreen](https://eucpilots.com/evergreen-docs/) to query for application version updates and store the result in JSON format. A workflow runs every 24-hours to retrieve available updates and commit changes to this repository.

View the data in the [Evergreen Workbench](https://eucpilots.com/workbench).

## Application update runners

[scripts/SkipApps.txt](scripts/SkipApps.txt) lists the Evergreen application names handled by the self-hosted runner, one name per line. Both update scripts read this shared file on each execution by default: [Update-Json.ps1](scripts/Update-Json.ps1) skips these applications, while [Update-JsonHosted.ps1](scripts/Update-JsonHosted.ps1) updates only these applications. The file is resolved relative to the scripts, regardless of the current working directory; a missing or unreadable file stops execution.

Edit this file to change the runner assignment. The existing `-SkipApps` and `-Apps` parameters remain available to override the list for an individual execution.

## Application failure issues

Both update workflows automatically manage one issue per Evergreen application, labelled `application-failure`. Exceptions, nonterminating retrieval errors, empty output, rate limiting, and JSON export failures create an issue containing current error details, timestamps, runner and Evergreen version, and a workflow/log link. Known environment secrets and common credential patterns are redacted from new error details; avoid putting other sensitive information in upstream error messages.

Repeated failures update the existing issue. Explicit success closes it with a recovery comment, even when the JSON data has not changed. A later failure reopens the same issue. Applications skipped or not reached during an interrupted run do not have their issues closed. Do not remove the label or hidden application/timestamp markers from managed issues; if duplicate managed issues are detected, reconciliation fails rather than creating another.

The update scripts accept an optional `-ReportPath` for current-run results. Workflows store these reports outside the repository and retain them as `application-results` artifacts for 14 days. A separate Ubuntu job runs [Sync-ApplicationIssues.ps1](scripts/Sync-ApplicationIssues.ps1), including after a partially failed update when a report is available. Only this job receives `issues: write`; update jobs retain `contents: write`. GitHub Issues must be enabled for the repository.

Reconciliation jobs share a concurrency group with `queue: max` and do not cancel running jobs (GitHub permits up to 100 pending jobs). Observation timestamps prevent delayed or replayed reports from overwriting newer issue state. GitHub API errors fail the reconciliation step and can be retried by rerunning the failed job.

Run the mocked tests locally with PowerShell and Pester 5:

```powershell
Invoke-Pester -Path ./scripts/tests/ApplicationIssues.Tests.ps1 -Output Detailed
```
