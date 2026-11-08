<#
    Queries each application in Evergreen and exports the result to JSON
#>
[CmdletBinding(SupportsShouldProcess = $true)]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute("PSAvoidUsingWriteHost", "")]
param(
    [ValidateNotNullOrEmpty()]
    [System.String] $Path,

    [ValidateNotNullOrEmpty()]
    [System.String[]] $Apps = (Get-Content -LiteralPath "$PSScriptRoot/SkipApps.txt" -ErrorAction "Stop"),

    [ValidateNotNullOrEmpty()]
    [System.String] $ReportPath
)

. "$PSScriptRoot/ApplicationResults.ps1"

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

$Report = Get-ApplicationReport
if ($ReportPath) {
    Save-ApplicationReport -Report $Report -ReportPath $ReportPath
}

# Walk-through each Evergreen app and export data to JSON files
foreach ($App in (Find-EvergreenApp | Where-Object { $_.Name -in $Apps } | Select-Object -ExpandProperty "Name" | Sort-Object)) {
    $Report.Results += Invoke-ApplicationUpdate -App $App -Path $Path
    if ($ReportPath) {
        Save-ApplicationReport -Report $Report -ReportPath $ReportPath
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
