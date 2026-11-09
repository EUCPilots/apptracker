[CmdletBinding()]
param([string] $ReportPath)

. "$PSScriptRoot/ApplicationResults.ps1"

function Invoke-ApplicationIssueApi {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Resource,
        [ValidateSet('GET', 'POST', 'PATCH')][string] $Method = 'GET',
        [hashtable] $Body
    )

    $Parameters = @{
        Uri         = "$env:GITHUB_API_URL/repos/$env:GITHUB_REPOSITORY/$Resource"
        Method      = $Method
        Headers     = @{
            Authorization          = "Bearer $env:GITHUB_TOKEN"
            Accept                 = 'application/vnd.github+json'
            'X-GitHub-Api-Version'  = '2022-11-28'
        }
        ErrorAction = 'Stop'
    }
    if ($Body) {
        $Parameters.Body = ConvertTo-Json $Body -Depth 6
        $Parameters.ContentType = 'application/json; charset=utf-8'
    }
    Invoke-RestMethod @Parameters
}

function Get-ApplicationIssuePage {
    [CmdletBinding()]
    [OutputType([object[]])]
    param([Parameter(Mandatory)][string] $Resource)

    $Separator = if ($Resource.Contains('?')) { '&' } else { '?' }
    $Page = 1
    do {
        $Response = Invoke-ApplicationIssueApi -Resource "${Resource}${Separator}per_page=100&page=$Page"
        $Items = @($Response)
        foreach ($Item in $Items) {
            $Item
        }
        $Page++
    } while ($Items.Count -eq 100)
}

function Sync-ApplicationIssue {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $ReportPath)

    $JsonParameters = @{ ErrorAction = 'Stop' }
    if ((Get-Command ConvertFrom-Json).Parameters.ContainsKey('DateKind')) {
        $JsonParameters.DateKind = 'String'
    }
    $Report = Get-Content -LiteralPath $ReportPath -Raw -ErrorAction Stop | ConvertFrom-Json @JsonParameters
    if (!$env:GITHUB_TOKEN -or !$env:GITHUB_REPOSITORY -or !$env:GITHUB_API_URL) {
        throw 'GITHUB_TOKEN, GITHUB_REPOSITORY and GITHUB_API_URL are required.'
    }
    if ($Report.SchemaVersion -ne 1 -or $Report.Repository -ne $env:GITHUB_REPOSITORY -or
        $Report.RunId -notmatch '^\d+$' -or $Report.RunAttempt -notmatch '^\d+$' -or
        !$Report.Workflow -or !$Report.Job -or !$Report.Runner -or !$Report.EvergreenVersion -or
        $null -eq $Report.Results) {
        throw 'Invalid application report metadata.'
    }
    $Names = @{}
    foreach ($Result in $Report.Results) {
        if ($Result.Application -notmatch '^[A-Za-z0-9_.-]+$' -or $Names.ContainsKey($Result.Application) -or
            $Result.Status -notin @('Success', 'Failure') -or
            ($Result.Status -eq 'Failure' -and (!$Result.Category -or !$Result.Details))) {
            throw 'Invalid or duplicate application result.'
        }
        if ($Result.ObservedAt -is [datetime]) {
            $Result.ObservedAt = $Result.ObservedAt.ToString('o', [cultureinfo]::InvariantCulture)
        }
        [datetimeoffset]::Parse($Result.ObservedAt, [cultureinfo]::InvariantCulture) | Out-Null
        $Names[$Result.Application] = $true
    }
    if (@($Report.Results).Count -eq 0) {
        Write-Information 'No applications were attempted; issues are unchanged.' -InformationAction Continue
        return
    }

    $Label = 'application-failure'
    $Labels = @(Get-ApplicationIssuePage -Resource 'labels')
    if ($Label -notin $Labels.name) {
        Invoke-ApplicationIssueApi -Resource 'labels' -Method POST -Body @{
            name = $Label; color = 'd73a4a'; description = 'Application update failures managed by App Tracker'
        } | Out-Null
    }
    $Issues = @(Get-ApplicationIssuePage -Resource "issues?state=all&labels=$Label" | Where-Object { !$_.pull_request })
    $Created = 0
    $Updated = 0
    $Closed = 0
    $Reopened = 0
    $RunUrl = "$env:GITHUB_SERVER_URL/$env:GITHUB_REPOSITORY/actions/runs/$($Report.RunId)/attempts/$($Report.RunAttempt)"

    foreach ($Result in $Report.Results) {
        $Marker = "<!-- apptracker:application:$($Result.Application) -->"
        $ApplicationIssues = @($Issues | Where-Object { $_.body -and $_.body.Contains($Marker) })
        if ($ApplicationIssues.Count -gt 1) {
            throw "Multiple managed issues exist for $($Result.Application); consolidate them before retrying."
        }
        $Issue = $ApplicationIssues | Select-Object -First 1
        $ObservedAt = [datetimeoffset]::Parse($Result.ObservedAt, [cultureinfo]::InvariantCulture)
        $FirstFailure = $Result.ObservedAt
        if ($Issue) {
            if ($Issue.body -match '<!-- apptracker:observed-at:(.*?) -->') {
                $PreviousObservation = [datetimeoffset]::Parse($Matches[1], [cultureinfo]::InvariantCulture)
                if ($ObservedAt -le $PreviousObservation) {
                    continue
                }
            }
            if ($Issue.body -match '<!-- apptracker:first-failure:(.*?) -->') {
                $FirstFailure = $Matches[1]
            }
        }
        $Metadata = "$Marker`n<!-- apptracker:first-failure:$FirstFailure -->`n<!-- apptracker:observed-at:$($Result.ObservedAt) -->"
        if ($Result.Status -eq 'Success') {
            if ($Issue) {
                $Body = $Issue.body -replace '<!-- apptracker:observed-at:.*? -->', "<!-- apptracker:observed-at:$($Result.ObservedAt) -->"
                if ($Issue.state -eq 'open') {
                    Invoke-ApplicationIssueApi -Resource "issues/$($Issue.number)/comments" -Method POST -Body @{
                        body = "Application ``$($Result.Application)`` updated and exported successfully at $($Result.ObservedAt). Recovered in [workflow run]($RunUrl)."
                    } | Out-Null
                    Invoke-ApplicationIssueApi -Resource "issues/$($Issue.number)" -Method PATCH -Body @{
                        state = 'closed'; state_reason = 'completed'; body = $Body
                    } | Out-Null
                    $Closed++
                }
                else {
                    Invoke-ApplicationIssueApi -Resource "issues/$($Issue.number)" -Method PATCH -Body @{ body = $Body } | Out-Null
                }
            }
            continue
        }

        $Details = Protect-ApplicationError -Message $Result.Details
        if ($Details.Length -gt 12000) {
            $Details = $Details.Substring(0, 12000) + "`n[Details truncated; see workflow logs.]"
        }
        $Details = [System.Net.WebUtility]::HtmlEncode($Details)
        $Body = @"
$Metadata
## Application update failed: $($Result.Application)

| Detail | Value |
| --- | --- |
| Failure category | $($Result.Category) |
| First failure | $FirstFailure |
| Latest failure | $($Result.ObservedAt) |
| Workflow / job | $($Report.Workflow) / $($Report.Job) |
| Run / attempt | $($Report.RunId) / $($Report.RunAttempt) |
| Runner | $($Report.Runner) |
| Evergreen version | $($Report.EvergreenVersion) |

### Failure details
<pre>$Details</pre>

[Workflow run and logs]($RunUrl)

This issue is updated automatically and closed after this application successfully exports JSON. A later failure reopens the same issue.
"@
        if ($Issue) {
            Invoke-ApplicationIssueApi -Resource "issues/$($Issue.number)" -Method PATCH -Body @{
                body = $Body; state = 'open'
            } | Out-Null
            if ($Issue.state -eq 'closed') { $Reopened++ } else { $Updated++ }
        }
        else {
            Invoke-ApplicationIssueApi -Resource 'issues' -Method POST -Body @{
                title = "Application update failed: $($Result.Application)"; body = $Body; labels = @($Label)
            } | Out-Null
            $Created++
        }
    }

    $Summary = "Application issues: $Created created, $Updated updated, $Reopened reopened, $Closed closed."
    Write-Information $Summary -InformationAction Continue
    if ($env:GITHUB_STEP_SUMMARY) {
        $Summary | Out-File -LiteralPath $env:GITHUB_STEP_SUMMARY -Append -Encoding utf8 -ErrorAction Stop
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    Sync-ApplicationIssue -ReportPath $ReportPath
}
