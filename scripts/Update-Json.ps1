<#
    Queries each application in Evergreen and exports the result to JSON
#>
[CmdletBinding(SupportsShouldProcess = $true)]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute("PSAvoidUsingWriteHost", "")]
param(
    [ValidateNotNullOrEmpty()]
    [System.String] $Path,

    [ValidateNotNullOrEmpty()]
    [System.String[]] $SkipApps = ("FreedomScientificFusion", "FreedomScientificJAWS", "FreedomScientificZoomText", "MicrosoftPowerAutomateDesktop", "OracleJava17", "OracleJava20", "OracleJava21", "OracleJava22", "OracleJava23", "OracleJava25", "OracleJava26", "Slack", "VideoLanVlcPlayer", "ClaudeDesktop", "OctopusDeployServer"),

    [ValidateNotNullOrEmpty()]
    [System.String[]] $MozillaApps = @("MozillaFirefox", "MozillaThunderbird")
)

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
    $ErrPath = [System.IO.Path]::Combine($Path, "$App.err")
    try {
        $params = @{
            Name          = $App
            ErrorAction   = "SilentlyContinue"
            WarningAction = "SilentlyContinue"
        }
        if ($App -in $MozillaApps) {
            $Manifest = Export-EvergreenManifest -Name $App
            $params.AppParams = @{ Language = $Manifest.Get.Download.FullLanguageList }
        }
        $Output = Get-EvergreenApp @params
    }
    catch {
        Write-Information -MessageData "$($PSStyle.Foreground.Cyan)Encountered an issue with: $App."
        Write-Information -MessageData "$($PSStyle.Foreground.Cyan)$($_.Exception.Message)"
        $_.Exception.Message | Out-File -FilePath $ErrPath -NoNewline -Encoding "utf8"
        $Output = $null
    }

    if ($null -eq $Output -or @($Output).Count -eq 0) {
        Write-Information -MessageData "$($PSStyle.Foreground.Cyan)Output from app is null: $App."
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
        Write-Information -MessageData "$($PSStyle.Foreground.Cyan)Skipping. GitHub API rate limited: $App."
        $Reason = "GitHub API rate limited."
        $Reason | Out-File -FilePath $ErrPath -NoNewline -Encoding "utf8"
        if ($env:GITHUB_ACTIONS -eq 'true') {
            Write-Host "::warning title=$App::$Reason"
        }
    }
    else {

        # Normalise URLs for SourceForge
        if ($Output[0].URI -match "sourceforge.net") {
            $Output = $Output | `
                ForEach-Object { $_.URI = $_.URI -replace [RegEx]::Match($_.URI, "https?://([^/]+)").Captures.Groups[1].Value, "ixpeering.dl.sourceforge.net"; $_ }
        }

        # Normalise URLs for various applications
        switch ($App) {
            "VideoLanVlcPlayer" {
                $Output = $Output | `
                    ForEach-Object { $_.URI = $_.URI -replace [RegEx]::Match($_.URI, "https?://([^/]+)").Captures.Groups[1].Value, "mirrors.middlendian.com"; $_ }
            }
        }

        # Sort and export to JSON
        ConvertTo-Json @($Output | `
                Sort-Object -Property @{ Expression = { [System.Version]$_.Version }; Descending = $true }, "Platform", "Type", "Architecture", "Channel", "Release", "Ring", "Language", "Product", "Branch", "JDK", "Title", "Edition" -ErrorAction "SilentlyContinue") | `
            Out-File -FilePath $([System.IO.Path]::Combine($Path, "$App.json")) -NoNewline -Encoding "utf8" -Verbose

        # Remove variable
        Remove-Variable -Name "Output" -ErrorAction "SilentlyContinue"

        # Remove any leftover .err file from previous runs
        Remove-Item -Path $ErrPath -ErrorAction "SilentlyContinue"
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
