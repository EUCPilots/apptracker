<#
    Gathers URLs from Evergreen and the App Tracker and posts them to the KV store
#>

# Read the application manifests and output URLs as a PSObject
# $UrlMatch = "(http|https):\/\/(.*[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+)/"
# $UrlMatch = "(?:(?:http|https):\/\/)(.*[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+)"
# Select-String -Path "/Users/aaron/projects/evergreen/Evergreen/Manifests/MicrosoftEdge.json" -Pattern $UrlMatch

$Namespace = "037069e7da3e4944be2cbc97c92409a5"
$UrlMatch = 'https?://([a-z0-9](?:[a-z0-9._:-]*[a-z0-9])?|\[[a-f0-9:.]+\](?::[0-9]+)?)(?:[/?#]|(?=["\s,}]|$))'
$EvergreenManifests = "/Users/aaron/projects/evergreen/Evergreen/Manifests"
$AppTrackerJson = "/Users/aaron/projects/apptracker/json"

# Get endpoint URLs from Evergreen manifests and post to the version endpoint
$Endpoints = Get-ChildItem -Path $EvergreenManifests -Recurse -Include "*.json" | ForEach-Object {
    [PSCustomObject]@{
        Application = $_.BaseName
        Endpoints   = @(((((Select-String -Path $_.FullName -Pattern $UrlMatch).Matches.Value | `
                            Select-Object -Unique | `
                            Sort-Object) -replace "http://|https://", "").TrimEnd("/|#|`",|`"")))
        Ports       = @(((((Select-String -Path $_.FullName -Pattern $UrlMatch).Matches.Value | Select-Object -Unique))) | ForEach-Object {
                if ($_ -match "http://") { "80" }
                if ($_ -match "https://") { "443" }
            } | Select-Object -Unique | Sort-Object -Descending)
    }
}
$Endpoints | ConvertTo-Json | Out-File -FilePath "./Endpoints.json" -Encoding "Utf8" -NoNewline
wrangler kv key put "endpoints-versions" --path="./Endpoints.json" --namespace-id=$Namespace
Remove-Item -Path "./Endpoints.json"

# Get endpoint URLs for downloads from the Evergreen AppTracker and port to the downloads endpoint
$Endpoints = Get-ChildItem -Path $AppTrackerJson -Recurse -Include "*.json" | ForEach-Object {
    [PSCustomObject]@{
        Application = $_.BaseName
        Endpoints   = @(((((Select-String -Path $_.FullName -Pattern $UrlMatch).Matches.Value | `
                            Select-Object -Unique | `
                            Sort-Object) -replace "http://|https://", "").TrimEnd("/|#|`",|`"")))
        Ports       = @(((((Select-String -Path $_.FullName -Pattern $UrlMatch).Matches.Value | Select-Object -Unique))) | ForEach-Object {
                if ($_ -match "http://") { "80" }
                if ($_ -match "https://") { "443" }
            } | Select-Object -Unique | Sort-Object -Descending)
    }
}
$Endpoints | ConvertTo-Json | Out-File -FilePath "./Endpoints.json" -Encoding "Utf8" -NoNewline
wrangler kv key put "endpoints-downloads" --path="./Endpoints.json" --namespace-id=$Namespace
Remove-Item -Path "./Endpoints.json"
