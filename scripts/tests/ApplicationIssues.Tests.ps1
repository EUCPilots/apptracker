BeforeAll {
    . "$PSScriptRoot/../ApplicationResults.ps1"
    . "$PSScriptRoot/../Sync-ApplicationIssues.ps1"

    function Get-EvergreenApp {
        [CmdletBinding()]
        param([string] $Name)
    }

    function Find-EvergreenApp {
        [CmdletBinding()]
        param()
    }
}

Describe 'Application update results' {
    BeforeEach {
        Mock Get-EvergreenApp { [pscustomobject]@{ Version = '1.2.3'; URI = 'https://example.org/app.exe' } }
        $AppPath = Join-Path $TestDrive ([guid]::NewGuid().ToString())
        New-Item -Path $AppPath -ItemType Directory -Force | Out-Null
    }

    It 'exports array JSON and removes a stale error on success, including unchanged data' {
        'stale error' | Set-Content "$AppPath/TestApp.err"
        $First = Invoke-ApplicationUpdate -App TestApp -Path $AppPath
        $First.Status | Should -Be Success
        Test-Path "$AppPath/TestApp.err" | Should -BeFalse
        $Json = Get-Content "$AppPath/TestApp.json" -Raw
        $Json.TrimStart().StartsWith('[') | Should -BeTrue
        (Invoke-ApplicationUpdate -App TestApp -Path $AppPath).Status | Should -Be Success
        Get-Content "$AppPath/TestApp.json" -Raw | Should -Be $Json
    }

    It 'records fresh details for empty output rather than reusing stale errors' {
        Mock Get-EvergreenApp { $null }
        'old error' | Set-Content "$AppPath/TestApp.err"
        $Result = Invoke-ApplicationUpdate -App TestApp -Path $AppPath -WarningAction SilentlyContinue
        $Result.Status | Should -Be Failure
        $Result.Category | Should -Be EmptyOutput
        $Result.Details | Should -Be 'Output from app was null or empty.'
        Get-Content "$AppPath/TestApp.err" -Raw | Should -Be $Result.Details
    }

    It 'records terminating errors and preserves previous JSON' {
        Mock Get-EvergreenApp { throw 'upstream unavailable' }
        'previous JSON' | Set-Content "$AppPath/TestApp.json" -NoNewline
        $Result = Invoke-ApplicationUpdate -App TestApp -Path $AppPath -WarningAction SilentlyContinue
        $Result.Category | Should -Be Exception
        $Result.Details | Should -Be 'upstream unavailable'
        Get-Content "$AppPath/TestApp.json" -Raw | Should -Be 'previous JSON'
    }

    It 'treats nonterminating errors with partial output as failures' {
        Mock Get-EvergreenApp {
            Write-Error 'partial retrieval failure'
            [pscustomobject]@{ Version = '1.2.3' }
        }
        $Result = Invoke-ApplicationUpdate -App TestApp -Path $AppPath -WarningAction SilentlyContinue
        $Result.Status | Should -Be Failure
        $Result.Details | Should -Match 'partial retrieval failure'
    }

    It 'includes warnings when output is empty' {
        Mock Get-EvergreenApp { Write-Warning 'upstream timeout' }
        (Invoke-ApplicationUpdate -App TestApp -Path $AppPath -WarningAction SilentlyContinue).Details | Should -Match 'upstream timeout'
    }

    It 'treats rate limiting as failure' {
        Mock Get-EvergreenApp { [pscustomobject]@{ Version = 'RateLimited' } }
        (Invoke-ApplicationUpdate -App TestApp -Path $AppPath -WarningAction SilentlyContinue).Category | Should -Be RateLimited
    }

    It 'records export errors without reporting success' {
        New-Item -Path "$AppPath/TestApp.json" -ItemType Directory | Out-Null
        $Result = Invoke-ApplicationUpdate -App TestApp -Path $AppPath -WarningAction SilentlyContinue
        $Result.Status | Should -Be Failure
        $Result.Category | Should -Be ExportFailure
        $Result.Details | Should -Not -BeNullOrEmpty
    }

    It 'still reports a failure when writing the error file also fails' {
        Mock Get-EvergreenApp { throw 'upstream unavailable' }
        New-Item -Path "$AppPath/TestApp.err" -ItemType Directory | Out-Null
        $Result = Invoke-ApplicationUpdate -App TestApp -Path $AppPath -WarningAction SilentlyContinue
        $Result.Status | Should -Be Failure
        $Result.Details | Should -Match 'Could not write error file:'
    }

    It 'preserves runner-specific URL normalisation' {
        Mock Get-EvergreenApp { [pscustomobject]@{ Version = '1.2.3'; URI = 'https://sourceforge.net/app.exe' } }
        Invoke-ApplicationUpdate -App TestApp -Path $AppPath -NormalizeUrls | Out-Null
        (Get-Content "$AppPath/TestApp.json" -Raw | ConvertFrom-Json)[0].URI | Should -Be 'https://ixpeering.dl.sourceforge.net/app.exe'
        Invoke-ApplicationUpdate -App TestApp -Path $AppPath | Out-Null
        (Get-Content "$AppPath/TestApp.json" -Raw | ConvertFrom-Json)[0].URI | Should -Be 'https://sourceforge.net/app.exe'
    }

    It 'persists only explicitly observed applications in a partial report' {
        $Report = Get-ApplicationReport
        $Report.Results += Invoke-ApplicationUpdate -App TestApp -Path $AppPath
        Save-ApplicationReport -Report $Report -ReportPath "$TestDrive/results.json"
        $Saved = Get-Content "$TestDrive/results.json" -Raw | ConvertFrom-Json
        @($Saved.Results).Count | Should -Be 1
        $Saved.Results[0].Application | Should -Be TestApp
    }

    It 'redacts authentication and sensitive URL parameters' {
        Protect-ApplicationError 'Bearer abcdef https://user:pass@example.org/?token=abc&api_key=def' |
            Should -Be 'Bearer [REDACTED] https://[REDACTED]@example.org/?token=[REDACTED]&api_key=[REDACTED]'
    }

    It 'preserves escaped GitHub Actions warning annotations' {
        $Previous = $env:GITHUB_ACTIONS
        try {
            $env:GITHUB_ACTIONS = 'true'
            Mock Get-EvergreenApp { throw "100% failure`nsecond line" }
            $Information = @()
            Invoke-ApplicationUpdate -App TestApp -Path $AppPath -WarningAction SilentlyContinue -InformationVariable Information | Out-Null
            ($Information | ForEach-Object { $_.MessageData }) | Should -Contain '::warning title=TestApp::100%25 failure%0Asecond line'
        }
        finally {
            $env:GITHUB_ACTIONS = $Previous
        }
    }
}

Describe 'Application runner integration' {
    BeforeEach {
        $script:RunnerSummary = $env:GITHUB_STEP_SUMMARY
        $env:GITHUB_STEP_SUMMARY = "$TestDrive/runner-summary.md"
        $RunnerPath = Join-Path $TestDrive ([guid]::NewGuid().ToString())
        New-Item -Path $RunnerPath -ItemType Directory | Out-Null
        '[]' | Set-Content "$RunnerPath/NormalApp.json"
        '[]' | Set-Content "$RunnerPath/HostedApp.json"
        'stale hosted error' | Set-Content "$RunnerPath/HostedApp.err"
        Mock Find-EvergreenApp {
            [pscustomobject]@{ Name = 'NormalApp' }
            [pscustomobject]@{ Name = 'HostedApp' }
        }
        Mock Get-EvergreenApp { [pscustomobject]@{ Version = '1.2.3'; URI = 'https://example.org/app.exe' } }
        Mock Import-Module {} -ParameterFilter { $Name -eq 'Evergreen' }
    }

    AfterEach {
        $env:GITHUB_STEP_SUMMARY = $script:RunnerSummary
    }

    It 'reports only non-skipped applications on the standard runner' {
        & "$PSScriptRoot/../Update-Json.ps1" -Path $RunnerPath -SkipApps HostedApp -ReportPath "$TestDrive/standard-results.json"
        $Report = Get-Content "$TestDrive/standard-results.json" -Raw | ConvertFrom-Json
        @($Report.Results).Count | Should -Be 1
        $Report.Results[0].Application | Should -Be NormalApp
        $Report.Results[0].Status | Should -Be Success
        Test-Path "$RunnerPath/HostedApp.err" | Should -BeTrue
        Test-Path "$TestDrive/standard-results.json.tmp" | Should -BeFalse
    }

    It 'reports only assigned applications on the self-hosted runner' {
        & "$PSScriptRoot/../Update-JsonHosted.ps1" -Path $RunnerPath -Apps HostedApp -ReportPath "$TestDrive/hosted-results.json"
        $Report = Get-Content "$TestDrive/hosted-results.json" -Raw | ConvertFrom-Json
        @($Report.Results).Count | Should -Be 1
        $Report.Results[0].Application | Should -Be HostedApp
        $Report.Results[0].Status | Should -Be Success
        Test-Path "$RunnerPath/HostedApp.err" | Should -BeFalse
        Test-Path "$TestDrive/hosted-results.json.tmp" | Should -BeFalse
    }
}

Describe 'Issue reconciliation' {
    BeforeEach {
        $script:SavedEnvironment = @{}
        foreach ($Name in @('GITHUB_TOKEN', 'GITHUB_REPOSITORY', 'GITHUB_API_URL', 'GITHUB_SERVER_URL', 'GITHUB_STEP_SUMMARY')) {
            $script:SavedEnvironment[$Name] = [Environment]::GetEnvironmentVariable($Name)
        }
        $env:GITHUB_TOKEN = 'fake-test-token'
        $env:GITHUB_REPOSITORY = 'test/apptracker'
        $env:GITHUB_API_URL = 'https://api.github.com'
        $env:GITHUB_SERVER_URL = 'https://github.com'
        $env:GITHUB_STEP_SUMMARY = "$TestDrive/summary.md"
        $script:Report = @{
            SchemaVersion = 1; Repository = 'test/apptracker'; Workflow = 'Update apps'
            Job = 'update-pwsh'; RunId = '123'; RunAttempt = '2'; Runner = 'Windows/X64'
            EvergreenVersion = '1.0.0'
            Results = @(@{
                Application = 'TestApp'; Status = 'Failure'; Category = 'Exception'
                Details = 'upstream failure'; ObservedAt = '2026-11-08T03:00:00Z'
            })
        }
        $script:Issues = @()
        $script:Calls = [System.Collections.Generic.List[object]]::new()
        Mock Invoke-ApplicationIssueApi {
            param($Resource, $Method, $Body)
            $script:Calls.Add(@{ Resource = $Resource; Method = $Method; Body = $Body })
            if ($Resource.StartsWith('labels?')) { return [pscustomobject]@{ name = 'application-failure' } }
            if ($Resource.StartsWith('issues?')) { return $script:Issues }
        }
        function Invoke-TestSync {
            $script:Report | ConvertTo-Json -Depth 6 | Set-Content "$TestDrive/results.json"
            Sync-ApplicationIssue -ReportPath "$TestDrive/results.json"
        }
        function Set-TestIssue {
            param([string] $State = 'open', [string] $App = 'TestApp')
            $script:Issues = @([pscustomobject]@{
                number = 42; state = $State
                body = "<!-- apptracker:application:$App -->`n<!-- apptracker:first-failure:2026-11-01T00:00:00Z -->`n<!-- apptracker:observed-at:2026-11-07T00:00:00Z -->"
            })
        }
    }

    AfterEach {
        foreach ($Name in $script:SavedEnvironment.Keys) {
            [Environment]::SetEnvironmentVariable($Name, $script:SavedEnvironment[$Name])
        }
    }

    It 'creates exactly one labelled issue with failure and run details' {
        Invoke-TestSync
        $Posts = @($script:Calls | Where-Object { $_.Method -eq 'POST' -and $_.Resource -eq 'issues' })
        $Posts.Count | Should -Be 1
        $Posts[0].Body.labels | Should -Contain 'application-failure'
        $Posts[0].Body.body | Should -Match 'upstream failure'
        $Posts[0].Body.body | Should -Match 'actions/runs/123/attempts/2'
        $Posts[0].Body.body | Should -Match 'Windows/X64'
        $Posts[0].Body.body | Should -Match 'Evergreen version'
    }

    It 'updates the existing issue regardless of its title and preserves first failure' {
        Set-TestIssue
        Invoke-TestSync
        @($script:Calls | Where-Object Method -eq POST).Count | Should -Be 0
        $Patch = $script:Calls | Where-Object Method -eq PATCH
        $Patch.Resource | Should -Be 'issues/42'
        $Patch.Body.body | Should -Match 'first-failure:2026-11-01T00:00:00Z'
    }

    It 'comments and closes on explicit success' {
        Set-TestIssue
        $script:Report.Results[0].Status = 'Success'
        Invoke-TestSync
        $Comment = $script:Calls | Where-Object Resource -eq 'issues/42/comments'
        $Comment.Body.body | Should -Match 'exported successfully'
        $Patch = $script:Calls | Where-Object Method -eq PATCH
        $Patch.Body.state | Should -Be closed
        $Patch.Body.state_reason | Should -Be completed
        $Patch.Body.body | Should -Match 'observed-at:2026-11-08T03:00:00Z'
    }

    It 'reopens the same issue on recurrence' {
        Set-TestIssue -State closed
        Invoke-TestSync
        @($script:Calls | Where-Object Method -eq POST).Count | Should -Be 0
        ($script:Calls | Where-Object Method -eq PATCH).Body.state | Should -Be open
    }

    It 'does not close an issue for an unattempted application' {
        Set-TestIssue -App SkippedApp
        $script:Report.Results[0].Status = 'Success'
        Invoke-TestSync
        @($script:Calls | Where-Object { $_.Method -in @('PATCH', 'POST') }).Count | Should -Be 0
    }

    It 'does not touch issues when no applications were attempted' {
        $script:Report.Results = @()
        Invoke-TestSync
        $script:Calls.Count | Should -Be 0
    }

    It 'ignores stale or replayed observations' -ForEach @('2026-11-06T00:00:00Z', '2026-11-07T00:00:00Z') {
        Set-TestIssue
        $script:Report.Results[0].ObservedAt = $_
        Invoke-TestSync
        @($script:Calls | Where-Object { $_.Method -in @('PATCH', 'POST') }).Count | Should -Be 0
    }

    It 'advances the observation on an already closed issue without another recovery comment' {
        Set-TestIssue -State closed
        $script:Report.Results[0].Status = 'Success'
        Invoke-TestSync
        @($script:Calls | Where-Object Method -eq POST).Count | Should -Be 0
        ($script:Calls | Where-Object Method -eq PATCH).Body.body | Should -Match 'observed-at:2026-11-08T03:00:00Z'
    }

    It 'ignores pull requests even if they contain the application marker' {
        Set-TestIssue
        $script:Issues[0] | Add-Member -NotePropertyName pull_request -NotePropertyValue @{ url = 'example' }
        Invoke-TestSync
        @($script:Calls | Where-Object { $_.Method -eq 'POST' -and $_.Resource -eq 'issues' }).Count | Should -Be 1
    }

    It 'refuses duplicate managed issues rather than opening another' {
        Set-TestIssue
        $script:Issues += $script:Issues[0]
        { Invoke-TestSync } | Should -Throw '*Multiple managed issues*'
        @($script:Calls | Where-Object { $_.Method -in @('PATCH', 'POST') }).Count | Should -Be 0
    }

    It 'validates the entire report before making API calls' -ForEach @('duplicate', 'status', 'metadata', 'timestamp') {
        switch ($_) {
            duplicate { $script:Report.Results += $script:Report.Results[0] }
            status { $script:Report.Results[0].Status = 'Skipped' }
            metadata { $script:Report.Repository = 'other/repo' }
            timestamp { $script:Report.Results[0].ObservedAt = 'invalid' }
        }
        { Invoke-TestSync } | Should -Throw
        $script:Calls.Count | Should -Be 0
    }

    It 'redacts secrets and safely renders error details within the issue size limit' {
        $script:Report.Results[0].Details = "fake-test-token <script> " + ('x' * 70000)
        Invoke-TestSync
        $Body = ($script:Calls | Where-Object { $_.Method -eq 'POST' -and $_.Resource -eq 'issues' }).Body.body
        $Body | Should -Not -Match 'fake-test-token'
        $Body | Should -Match '\[REDACTED\] &lt;script&gt;'
        $Body | Should -Match 'Details truncated'
        $Body.Length | Should -BeLessThan 65536
    }

    It 'creates the label when it is missing' {
        Mock Invoke-ApplicationIssueApi {} -ParameterFilter { $Resource -like 'labels?*' }
        Invoke-TestSync
        ($script:Calls | Where-Object Resource -eq labels).Body.name | Should -Be 'application-failure'
    }

    It 'surfaces API errors instead of reporting successful reconciliation' {
        Mock Invoke-ApplicationIssueApi { throw 'GitHub API unavailable' } -ParameterFilter { $Method -eq 'POST' }
        { Invoke-TestSync } | Should -Throw '*GitHub API unavailable*'
    }

    It 'retrieves every page rather than limiting issue matching to the first page' {
        Mock Invoke-ApplicationIssueApi {
            1..100 | ForEach-Object { [pscustomobject]@{ number = $_ } }
        } -ParameterFilter { $Resource -like '*page=1' }
        Mock Invoke-ApplicationIssueApi { [pscustomobject]@{ number = 101 } } -ParameterFilter { $Resource -like '*page=2' }
        $Items = @(Get-ApplicationIssuePage -Resource 'issues?state=all')
        $Items.Count | Should -Be 101
        $Items[-1].number | Should -Be 101
    }

    It 'maintains exactly one issue across failure, repeated failure, recovery and recurrence' {
        Mock Invoke-ApplicationIssueApi {
            param($Resource, $Method, $Body)
            $script:Calls.Add(@{ Resource = $Resource; Method = $Method; Body = $Body })
            if ($Resource.StartsWith('labels?')) { return [pscustomobject]@{ name = 'application-failure' } }
            if ($Resource.StartsWith('issues?')) { return $script:Issues }
            if ($Resource -eq 'issues' -and $Method -eq 'POST') {
                $script:Issues += [pscustomobject]@{ number = 42; state = 'open'; body = $Body.body }
            }
            if ($Resource -eq 'issues/42' -and $Method -eq 'PATCH') {
                $script:Issues[0].body = $Body.body
                if ($Body.state) { $script:Issues[0].state = $Body.state }
            }
        }
        Invoke-TestSync
        $script:Report.Results[0].ObservedAt = '2026-11-08T04:00:00Z'
        Invoke-TestSync
        $script:Issues.Count | Should -Be 1
        $script:Report.Results[0].ObservedAt = '2026-11-08T05:00:00Z'
        $script:Report.Results[0].Status = 'Success'
        Invoke-TestSync
        $script:Issues[0].state | Should -Be closed
        $script:Report.Results[0].ObservedAt = '2026-11-08T06:00:00Z'
        $script:Report.Results[0].Status = 'Failure'
        Invoke-TestSync
        $script:Issues.Count | Should -Be 1
        $script:Issues[0].state | Should -Be open
        @($script:Calls | Where-Object { $_.Method -eq 'POST' -and $_.Resource -eq 'issues' }).Count | Should -Be 1
        $script:Report.Results[0].ObservedAt = '2026-11-08T04:00:00Z'
        $script:Report.Results[0].Status = 'Success'
        Invoke-TestSync
        $script:Issues[0].state | Should -Be open
    }

}

Describe 'GitHub API transport' {
    BeforeEach {
        $script:TransportEnvironment = @{}
        foreach ($Name in @('GITHUB_TOKEN', 'GITHUB_REPOSITORY', 'GITHUB_API_URL')) {
            $script:TransportEnvironment[$Name] = [Environment]::GetEnvironmentVariable($Name)
        }
        $env:GITHUB_TOKEN = 'fake-test-token'
        $env:GITHUB_REPOSITORY = 'test/apptracker'
        $env:GITHUB_API_URL = 'https://api.github.com'
    }

    AfterEach {
        foreach ($Name in $script:TransportEnvironment.Keys) {
            [Environment]::SetEnvironmentVariable($Name, $script:TransportEnvironment[$Name])
        }
    }

    It 'uses authenticated JSON requests' {
        Mock Invoke-RestMethod { [pscustomobject]@{ number = 42 } }
        Invoke-ApplicationIssueApi -Resource issues -Method POST -Body @{ title = 'Test'; labels = @('application-failure') } | Out-Null
        Should -Invoke Invoke-RestMethod -Times 1 -Exactly -ParameterFilter {
            $Uri -eq 'https://api.github.com/repos/test/apptracker/issues' -and
            $Method -eq 'POST' -and $Headers.Authorization -eq 'Bearer fake-test-token' -and
            $ContentType -eq 'application/json; charset=utf-8' -and
            ($Body | ConvertFrom-Json).labels[0] -eq 'application-failure'
        }
    }

    It 'surfaces transport failures' {
        Mock Invoke-RestMethod { throw 'transport unavailable' }
        { Invoke-ApplicationIssueApi -Resource issues } | Should -Throw '*transport unavailable*'
    }
}

Describe 'Workflow integration' {
    It 'serializes reconciliation across both workflows without replacing pending jobs' {
        $Workflows = @('update-apps.yml', 'update-hosted.yml')
        foreach ($Workflow in $Workflows) {
            $Content = Get-Content "$PSScriptRoot/../../.github/workflows/$Workflow" -Raw
            $Content | Should -Match 'group: application-issue-reconciliation'
            $Content | Should -Match 'cancel-in-progress: false'
            $Content | Should -Match 'queue: max'
            $Content | Should -Match 'issues: write'
            $Content | Should -Match 'needs\..*\.outputs\.report-artifact'
            $Content | Should -Match 'if: \$\{\{ !cancelled\(\)'
            $Content | Should -Match 'ReportPath "\$env:RUNNER_TEMP/'
            $Content | Should -Match 'ReportPath \$Reports\[0\]\.FullName'
        }
    }
}
