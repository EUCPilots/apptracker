BeforeAll {
    $Workflow = Get-Content "$PSScriptRoot/../../.github/workflows/update-apps.yml" -Raw
    $WorkflowBlocks = [regex]::Matches(
        $Workflow,
        '(?ms)^          \$UrlMatch = .*?(?=^          \$Endpoints \| ConvertTo-Json)'
    )
    $LocalScript = Get-Content "$PSScriptRoot/../Get-Endpoints.ps1" -Raw
    $LocalPattern = [regex]::Match($LocalScript, '(?m)^\$UrlMatch = .*$').Value
    $LocalBlocks = [regex]::Matches(
        $LocalScript,
        '(?ms)^\$Endpoints = Get-ChildItem .*?(?=^\$Endpoints \| ConvertTo-Json)'
    )
}

Describe 'Endpoint extraction' {
    BeforeEach {
        New-Item -ItemType Directory -Path "$TestDrive/Manifests", "$TestDrive/json" -Force | Out-Null
    }

    It 'excludes regex fragments while retaining real endpoints and ports in <Source>' -TestCases @(
        @{ Source = 'versions workflow'; Index = 0; Local = $false }
        @{ Source = 'downloads workflow'; Index = 1; Local = $false }
        @{ Source = 'local versions script'; Index = 0; Local = $true }
        @{ Source = 'local downloads script'; Index = 1; Local = $true }
    ) {
        param($Source, $Index, $Local)

        $Manifest = @'
{
    "Source": "https://cyberduck.io/",
    "Get": {
        "Update": {
            "Uri": { "Release": "https://version.cyberduck.io/windows/changelog.rss" },
            "MatchUrlPath": "(?<=https://|http://)(.*)"
        }
    },
    "Other": ["http://downloads.example.org:8080/file", "https://cyberduck.io/"]
}
'@
        $Manifest | Set-Content "$TestDrive/Manifests/Cyberduck.json"
        $Manifest | Set-Content "$TestDrive/json/Cyberduck.json"

        if ($Local) {
            $EvergreenManifests = "$TestDrive/Manifests"
            $AppTrackerJson = "$TestDrive/json"
            $Block = "$LocalPattern`n$($LocalBlocks[$Index].Value)"
        }
        else {
            $Block = $WorkflowBlocks[$Index].Value.Replace('${{ github.workspace }}', $TestDrive)
        }
        $Result = & ([scriptblock]::Create("$Block`n,`$Endpoints"))

        $Result.Application | Should -Be 'Cyberduck'
        @($Result.Endpoints).Count | Should -Be 3
        $Result.Endpoints | Should -Contain 'cyberduck.io'
        $Result.Endpoints | Should -Contain 'version.cyberduck.io'
        $Result.Endpoints | Should -Contain 'downloads.example.org:8080'
        $Result.Endpoints | Should -Not -Contain ')(.*)'
        $Result.Ports | Should -Contain '80'
        $Result.Ports | Should -Contain '443'
    }

    It 'does not infer an HTTP port from the Cyberduck match expression' {
        @'
{
    "Source": "https://cyberduck.io/",
    "MatchUrlPath": "(?<=https://|http://)(.*)"
}
'@ | Set-Content "$TestDrive/Manifests/Cyberduck.json"
        $Block = $WorkflowBlocks[0].Value.Replace('${{ github.workspace }}', $TestDrive)
        $Result = & ([scriptblock]::Create("$Block`n,`$Endpoints"))

        @($Result.Endpoints).Count | Should -Be 1
        $Result.Endpoints | Should -Contain 'cyberduck.io'
        $Result.Ports | Should -Not -Contain '80'
        $Result.Ports | Should -Contain '443'
    }

    It 'retains host-only URLs, uppercase hostnames and IPv6 addresses' {
        @'
{
    "Site": "https://LTspice.analog.com",
    "IP": "http://[2001:db8::1]:8080/file",
    "Invalid": "http://)(.*)"
}
'@ | Set-Content "$TestDrive/Manifests/Cyberduck.json"
        $Block = $WorkflowBlocks[0].Value.Replace('${{ github.workspace }}', $TestDrive)
        $Result = & ([scriptblock]::Create("$Block`n,`$Endpoints"))

        @($Result.Endpoints).Count | Should -Be 2
        $Result.Endpoints | Should -Contain 'LTspice.analog.com'
        $Result.Endpoints | Should -Contain '[2001:db8::1]:8080'
        $Result.Ports | Should -Contain '80'
        $Result.Ports | Should -Contain '443'
    }
}
