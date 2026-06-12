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
        $_.Exception.Message | Out-File -FilePath $([System.IO.Path]::Combine($Path, "$App.err")) -NoNewline -Encoding "utf8"
        $Output = $null
    }

    if ($null -eq $Output) {
        Write-Host -Object "Output from app is null: $App." -ForegroundColor "Cyan"
        if (!(Test-Path -Path $([System.IO.Path]::Combine($Path, "$App.err")))) {
            "Output from last run on PowerShell Core was null." | Out-File -FilePath $([System.IO.Path]::Combine($Path, "$App.err")) -NoNewline -Encoding "utf8"
        }
    }
    elseif ("RateLimited" -in $Output.Version) {
        Write-Host -Object "Skipping. GitHub API rate limited: $App." -ForegroundColor "Cyan"
    }
    else {
        ConvertTo-Json @($Output | `
                Sort-Object -Property @{ Expression = { [System.Version]$_.Version }; Descending = $true }, "Platform", "Type", "Architecture", "Channel", "Release", "Ring", "Language", "Product", "Branch", "JDK", "Title", "Edition" -ErrorAction "SilentlyContinue") | `
            Out-File -FilePath $([System.IO.Path]::Combine($Path, "$App.json")) -NoNewline -Encoding "utf8" -Verbose
        Remove-Variable -Name "Output" -ErrorAction "SilentlyContinue"
    }
}
