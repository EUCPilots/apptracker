<#
    Queries each application in Evergreen and exports the result to JSON
#>
[CmdletBinding(SupportsShouldProcess = $true)]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute("PSAvoidUsingWriteHost", "")]
param(
    [ValidateNotNullOrEmpty()]
    [System.String] $Path,

    [ValidateNotNullOrEmpty()]
    [System.String[]] $Apps = ("FreedomScientificFusion", "FreedomScientificJAWS", "FreedomScientificZoomText", "OracleJava17", "OracleJava20", "OracleJava21", "OracleJava22", "OracleJava23", "OracleJava25", "Slack", "VideoLanVlcPlayer")
)

#region Functions
function Set-Culture {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param ([System.Globalization.CultureInfo] $Culture)
    process {
        if ($PSCmdlet.ShouldProcess($Culture, "Setting culture")) {
            [System.Threading.Thread]::CurrentThread.CurrentUICulture = $Culture
            [System.Threading.Thread]::CurrentThread.CurrentCulture = $Culture
        }
    }
}
#endregion

# Set culture so that we get correct date formats
Set-Culture -Culture "en-AU"

# Step through all apps and export result to JSON
Import-Module -Name "Evergreen" -Force

# Walk-through each Evergreen app and export data to JSON files
foreach ($App in (Find-EvergreenApp | Where-Object { $_.Name -in $Apps } | Select-Object -ExpandProperty "Name" | Sort-Object)) {
    $ErrPath = [System.IO.Path]::Combine($Path, "$App.err")
    try {
        $params = @{
            Name          = $App
            ErrorAction   = "SilentlyContinue"
            WarningAction = "SilentlyContinue"
        }
        $Output = Get-EvergreenApp @params
    }
    catch {
        Write-Host -Object "Encountered an issue with: $App." -ForegroundColor "Cyan"
        Write-Host -Object $_.Exception.Message -ForegroundColor "Cyan"
        $_.Exception.Message | Out-File -FilePath $ErrPath -NoNewline -Encoding "utf8"
        $Output = $null
    }

    if ($null -eq $Output -or @($Output).Count -eq 0) {
        Write-Host -Object "Output from app is null: $App." -ForegroundColor "Cyan"
        if (!(Test-Path -Path $ErrPath)) {
            "Output from last run on PowerShell Core was null." | Out-File -FilePath $ErrPath -NoNewline -Encoding "utf8"
        }
        $Reason = (Get-Content -Path $ErrPath -Raw -ErrorAction "SilentlyContinue").Trim()
        if ([string]::IsNullOrWhiteSpace($Reason)) {
            $Reason = "Output from app was null or empty."
        }
        if ($env:GITHUB_ACTIONS -eq 'true') {
            $CleanReason = $Reason.Replace("`r`n", " ").Replace("`n", " ")
            Write-Host "::warning title=$App::$CleanReason"
        }
    }
    elseif ("RateLimited" -in $Output.Version) {
        Write-Host -Object "Skipping. GitHub API rate limited: $App." -ForegroundColor "Cyan"
        $Reason = "GitHub API rate limited."
        $Reason | Out-File -FilePath $ErrPath -NoNewline -Encoding "utf8"
        if ($env:GITHUB_ACTIONS -eq 'true') {
            Write-Host "::warning title=$App::$Reason"
        }
    }
    else {
        ConvertTo-Json @($Output | `
                Sort-Object -Property @{ Expression = { [System.Version]$_.Version }; Descending = $true }, "Platform", "Type", "Architecture", "Channel", "Release", "Ring", "Language", "Product", "Branch", "JDK", "Title", "Edition" -ErrorAction "SilentlyContinue") | `
            Out-File -FilePath $([System.IO.Path]::Combine($Path, "$App.json")) -NoNewline -Encoding "utf8" -Verbose
        Remove-Item -Path $ErrPath -ErrorAction "SilentlyContinue"
        Remove-Variable -Name "Output" -ErrorAction "SilentlyContinue"
    }
}

# Write summary to $env:GITHUB_STEP_SUMMARY if running in GitHub Actions
if ($env:GITHUB_STEP_SUMMARY) {
    $ErrFiles = Get-ChildItem -Path $Path -Filter "*.err" -ErrorAction "SilentlyContinue" | Where-Object { $_.BaseName -in $Apps } | Sort-Object -Property "BaseName"

    $SummaryMarkdown = "### ⚠️ Self-Hosted Applications with Errors or No Output ($($ErrFiles.Count))`n`n"
    if ($ErrFiles.Count -gt 0) {
        $SummaryMarkdown += "| Application | Reason / Details |`n"
        $SummaryMarkdown += "| :--- | :--- |`n"
        foreach ($File in $ErrFiles) {
            $AppName = $File.BaseName
            $Reason = (Get-Content -Path $File.FullName -Raw -ErrorAction "SilentlyContinue").Trim()
            if ([string]::IsNullOrWhiteSpace($Reason)) {
                $Reason = "Output was null or empty."
            }
            $CleanReason = $Reason.Replace("`r`n", " ").Replace("`n", " ").Replace("|", "&#124;")
            $SummaryMarkdown += "| ``$AppName`` | $CleanReason |`n"
        }
    }
    else {
        $SummaryMarkdown = "### Self-Hosted Applications Update Summary`n`nAll self-hosted applications updated successfully.`n"
    }
    $SummaryMarkdown | Out-File -FilePath $env:GITHUB_STEP_SUMMARY -Append -Encoding "utf8"
}
