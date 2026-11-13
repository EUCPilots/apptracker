# App Tracker

An application version tracker that uses [Evergreen](https://eucpilots.com/evergreen-docs/) to query for application version updates and store the result in JSON format. A workflow runs every 24-hours to retrieve available updates and commit changes to this repository.

View the data in the [Evergreen Workbench](https://eucpilots.com/workbench).

## Application update runners

[scripts/SkipApps.txt](scripts/SkipApps.txt) lists the Evergreen application names handled by the self-hosted runner, one name per line. Both update scripts read this shared file on each execution by default: [Update-Json.ps1](scripts/Update-Json.ps1) skips these applications, while [Update-JsonHosted.ps1](scripts/Update-JsonHosted.ps1) updates only these applications. The file is resolved relative to the scripts, regardless of the current working directory; a missing or unreadable file stops execution.

Edit this file to change the runner assignment. The existing `-SkipApps` and `-Apps` parameters remain available to override the list for an individual execution.

## Application failure issues

Both update workflows automatically manage one issue per Evergreen application, labelled `application-failure`. Exceptions, nonterminating retrieval errors, empty output, rate limiting, and JSON export failures create an issue containing current error details, timestamps, runner and Evergreen version, and a workflow/log link. Known environment secrets and common credential patterns are redacted from new error details; avoid putting other sensitive information in upstream error messages.

Repeated failures update the existing issue. Explicit success closes it with a recovery comment, even when the JSON data has not changed. A later failure reopens the same issue. Applications skipped or not reached during an interrupted run do not have their issues closed. Do not remove the label or hidden application/timestamp markers from managed issues. If duplicate managed issues are detected, reconciliation reuses the oldest issue and closes open duplicates with a comment linking to it, rather than creating another. Reports older than any matching issue's observation are ignored; replaying the latest report can finish duplicate cleanup after a partial API failure without repeating the primary update.

The update scripts accept an optional `-ReportPath` for current-run results. Workflows store these reports outside the repository and retain them as `application-results` artifacts for 14 days. A separate Ubuntu job runs [Sync-ApplicationIssues.ps1](scripts/Sync-ApplicationIssues.ps1), including after a partially failed update when a report is available. Only this job receives `issues: write`; update jobs retain `contents: write`. GitHub Issues must be enabled for the repository.

Reconciliation jobs share a concurrency group with `queue: max` and do not cancel running jobs (GitHub permits up to 100 pending jobs). Observation timestamps prevent delayed or replayed reports from overwriting newer issue state. GitHub API errors fail the reconciliation step and can be retried by rerunning the failed job.

Run the mocked tests locally with PowerShell and Pester 5:

```powershell
Invoke-Pester -Path ./scripts/tests/ApplicationIssues.Tests.ps1 -Output Detailed
```

## Daily Evergreen API validation

[Validate Evergreen API](.github/workflows/validate-api.yml) runs daily at 14:23 UTC and supports manual dispatch. GitHub scheduled runs can be delayed. Set the repository Actions secret `API_TEST_USERAGENT` to the custom User-Agent required by the API. GitHub Issues must be enabled; the workflow uses `GITHUB_TOKEN` with `issues: write` and read-only repository access.

[validate_evergreen_api.py](scripts/validate_evergreen_api.py) checks the production [Evergreen API 1.6.0](https://app.swaggerhub.com/apis/stealthpuppy/evergreen-api/1.6.0): `/`, `/app`, `/apps`, `/health`, `/endpoints`, `/endpoints/versions`, `/endpoints/downloads`, and `/app/{appId}` for every app discovered from `/apps`. If discovery fails, it attempts the known apps in [supported-apps.json](json/supported-apps.json), but the run still fails. A malformed catalog does not prevent checking its valid app identifiers.

Validation checks HTTP status, JSON content type, required fields, nonempty collections and strings, download/vendor URLs, endpoint hostnames, and health/binding status. Null values, including nested or optional fields, fail validation except for the explicitly allowlisted known nulls in the listed application release fields. The informational `/`, `/app`, and `/endpoints` routes intentionally support the contract's documented 404/400 responses as well as its compatibility 200 responses, provided their JSON bodies are valid. Application releases can be objects or arrays; every release is checked. `Size` accepts nonnegative numbers (including fractions), numeric strings with optional surrounding whitespace, and size strings with byte units (`B`, `KB`–`EB`, or `KiB`–`EiB`, case-insensitive), such as `"343.52 MB"`, matching existing Evergreen releases despite the published integer schema.

Requests are sequential with a 15-second timeout and no retries. The four throttled endpoints (`MicrosoftEdgeWebView2Runtime`, `MicrosoftEdge`, `AdobeAcrobatReaderDC`, and `CitrixWorkspaceApp`) are spaced at least 10 seconds apart, even after a failed request or on a repeat query.

All failures are collected in one managed issue titled **Evergreen API validation failed**, with endpoint names, validation/HTTP errors, observation time, and a workflow/log link. Repeated failures update it; a fully successful run comments and closes it; subsequent failures reopen it. Unrelated application-failure issues are unchanged. Do not remove both the title and hidden API-validation marker. Duplicate open managed issues stop reconciliation without creating another issue. Whole-run concurrency prevents overlapping issue creation.

Successful endpoint payloads are only held in memory: they are not logged, exported, uploaded as artifacts, or committed. Logs contain counts and failure diagnostics; the job summary contains counts only. Known secret values are redacted from failure diagnostics. API failures and GitHub reconciliation errors fail the workflow.

Run the mocked validator tests locally with Python (standard library only; no API requests or credentials required):

```sh
python3 -m unittest discover -s scripts/tests -p 'test_evergreen_api.py' -v
```
