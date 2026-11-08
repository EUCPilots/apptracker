<#
    Queries each application in Evergreen and exports the result to JSON
#>
[CmdletBinding(SupportsShouldProcess = $true)]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute("PSAvoidUsingWriteHost", "")]
param(
    [ValidateNotNullOrEmpty()]
    [System.String] $Path,

    [ValidateNotNullOrEmpty()]
    [System.String[]] $SkipApps = (Get-Content -LiteralPath "$PSScriptRoot\SkipApps.txt" -ErrorAction "Stop"),

    [ValidateNotNullOrEmpty()]
    [System.String] $ReportPath
)

. "$PSScriptRoot/ApplicationResults.ps1"
$Report = Get-ApplicationReport
if ($ReportPath) {
    Save-ApplicationReport -Report $Report -ReportPath $ReportPath
}

# Configure the environment
$InformationPreference = [System.Management.Automation.ActionPreference]::Continue
$ProgressPreference = [System.Management.Automation.ActionPreference]::SilentlyContinue
[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072

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

# Remove extra files for apps that have been removed from Evergreen
$Files = Get-ChildItem -Path $Path -Filter "*.json" | Select-Object -ExpandProperty "Basename"
$Apps = Find-EvergreenApp | Select-Object -ExpandProperty "Name"
Compare-Object -ReferenceObject $Files -DifferenceObject $Apps | `
    Select-Object -ExpandProperty "InputObject" | `
    ForEach-Object {
        Remove-Item -Path $([System.IO.Path]::Combine($Path, "$($_).json")) -ErrorAction "SilentlyContinue"
        Remove-Item -Path $([System.IO.Path]::Combine($Path, "$($_).err")) -ErrorAction "SilentlyContinue"
    }

# Walk-through each Evergreen app and export data to JSON files
foreach ($App in (Find-EvergreenApp | Where-Object { $_.Name -notin $SkipApps } | Select-Object -ExpandProperty "Name" | Sort-Object)) {
    $Report.Results += Invoke-ApplicationUpdate -App $App -Path $Path -NormalizeUrls
    if ($ReportPath) {
        Save-ApplicationReport -Report $Report -ReportPath $ReportPath
    }
}

# Write summary to $env:GITHUB_STEP_SUMMARY if running in GitHub Actions
if ($env:GITHUB_STEP_SUMMARY) {
    $ErrFiles = Get-ChildItem -Path $Path -Filter "*.err" -ErrorAction "SilentlyContinue" | Sort-Object -Property "BaseName"

    $SummaryMarkdown = "### ⚠️ Applications with Errors or No Output ($($ErrFiles.Count))`n`n"
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
        $SummaryMarkdown = "### Applications Update Summary`n`nAll applications updated successfully.`n"
    }
    $SummaryMarkdown | Out-File -FilePath $env:GITHUB_STEP_SUMMARY -Append -Encoding "utf8"
}
