function Protect-ApplicationError {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $Message)

    foreach ($Variable in (Get-ChildItem Env: | Where-Object { $_.Name -match 'TOKEN|SECRET|PASSWORD|PASSPHRASE|KEY' -and $_.Value.Length -ge 4 })) {
        $Message = $Message.Replace($Variable.Value, '[REDACTED]')
    }
    $Message = $Message -replace '(?i)(Bearer\s+)\S+', '$1[REDACTED]'
    $Message = $Message -replace '(?i)(https?://)[^/\s@]+:[^/\s@]+@', '$1[REDACTED]@'
    $Message = $Message -replace '(?i)([?&](?:token|access_token|api_key|key|password|secret)=)[^&\s]+', '$1[REDACTED]'
    return $Message
}

function Get-ApplicationReport {
    [CmdletBinding()]
    [OutputType([System.Collections.Specialized.OrderedDictionary])]
    param()

    return [ordered]@{
        SchemaVersion    = 1
        Repository       = $env:GITHUB_REPOSITORY
        Workflow         = $env:GITHUB_WORKFLOW
        Job              = $env:GITHUB_JOB
        RunId            = $env:GITHUB_RUN_ID
        RunAttempt       = $env:GITHUB_RUN_ATTEMPT
        RunUrl           = "$env:GITHUB_SERVER_URL/$env:GITHUB_REPOSITORY/actions/runs/$env:GITHUB_RUN_ID"
        Runner           = "$env:RUNNER_OS/$env:RUNNER_ARCH"
        EvergreenVersion = [string](Get-Module Evergreen | Select-Object -First 1 -ExpandProperty Version)
        Results          = @()
    }
}

function Save-ApplicationReport {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][System.Collections.IDictionary] $Report,
        [Parameter(Mandatory)][string] $ReportPath
    )

    $TemporaryPath = "$ReportPath.tmp"
    try {
        $Report | ConvertTo-Json -Depth 6 | Out-File -LiteralPath $TemporaryPath -Encoding utf8 -Force -ErrorAction Stop
        Move-Item -LiteralPath $TemporaryPath -Destination $ReportPath -Force -ErrorAction Stop
    }
    finally {
        if (Test-Path -LiteralPath $TemporaryPath) {
            Remove-Item -LiteralPath $TemporaryPath -Force -ErrorAction Stop
        }
    }
}

function Invoke-ApplicationUpdate {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $App,
        [Parameter(Mandatory)][string] $Path,
        [switch] $NormalizeUrls
    )

    $ErrPath = Join-Path $Path "$App.err"
    $Category = 'Exception'
    try {
        $AppErrors = @()
        $AppWarnings = @()
        $Output = @(Get-EvergreenApp -Name $App -ErrorAction SilentlyContinue -WarningAction SilentlyContinue -ErrorVariable AppErrors -WarningVariable AppWarnings | Where-Object { $null -ne $_ })
        if ($AppErrors.Count -gt 0) {
            $Reason = ($AppErrors | ForEach-Object { $_.ToString() }) -join "`n"
        }
        elseif ($Output.Count -eq 0) {
            $Category = 'EmptyOutput'
            $Reason = 'Output from app was null or empty.'
            if ($AppWarnings.Count -gt 0) {
                $Reason += "`n" + ($AppWarnings -join "`n")
            }
        }
        elseif ('RateLimited' -in $Output.Version) {
            $Category = 'RateLimited'
            $Reason = 'GitHub API rate limited.'
        }
        else {
            $Category = 'ExportFailure'
            if ($NormalizeUrls) {
                if ($Output[0].URI -match 'sourceforge.net') {
                    $Output = @($Output | ForEach-Object {
                        $_.URI = $_.URI -replace [regex]::Match($_.URI, 'https?://([^/]+)').Groups[1].Value, 'ixpeering.dl.sourceforge.net'
                        $_
                    })
                }
                if ($App -eq 'VideoLanVlcPlayer') {
                    $Output = @($Output | ForEach-Object {
                        $_.URI = $_.URI -replace [regex]::Match($_.URI, 'https?://([^/]+)').Groups[1].Value, 'mirrors.middlendian.com'
                        $_
                    })
                }
            }
            ConvertTo-Json @($Output | Sort-Object -Property @{ Expression = { [System.Version]$_.Version }; Descending = $true }, 'Platform', 'Type', 'Architecture', 'Channel', 'Release', 'Ring', 'Language', 'Product', 'Branch', 'JDK', 'Title', 'Edition' -ErrorAction SilentlyContinue) |
                Out-File -LiteralPath (Join-Path $Path "$App.json") -NoNewline -Encoding utf8 -Verbose -ErrorAction Stop
            if (Test-Path -LiteralPath $ErrPath) {
                Remove-Item -LiteralPath $ErrPath -Force -ErrorAction Stop
            }
            return [pscustomobject]@{
                Application = $App
                Status      = 'Success'
                Category    = $null
                Details     = $null
                ObservedAt  = [datetimeoffset]::UtcNow.ToString('o')
            }
        }
    }
    catch {
        $Reason = $_.Exception.Message
    }

    $Reason = Protect-ApplicationError -Message $Reason
    try {
        $Reason | Out-File -LiteralPath $ErrPath -NoNewline -Encoding utf8 -ErrorAction Stop
    }
    catch {
        $Reason += "`nCould not write error file: " + (Protect-ApplicationError -Message $_.Exception.Message)
    }
    Write-Warning "$App - $Reason"
    if ($env:GITHUB_ACTIONS -eq 'true') {
        $Annotation = $Reason.Replace('%', '%25').Replace("`r", '%0D').Replace("`n", '%0A')
        Write-Information "::warning title=$App::$Annotation" -InformationAction Continue
    }
    return [pscustomobject]@{
        Application = $App
        Status      = 'Failure'
        Category    = $Category
        Details     = $Reason
        ObservedAt  = [datetimeoffset]::UtcNow.ToString('o')
    }
}
