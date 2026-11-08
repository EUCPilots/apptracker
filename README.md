# App Tracker

An application version tracker that uses [Evergreen](https://eucpilots.com/evergreen-docs/) to query for application version updates and store the result in JSON format. A workflow runs every 24-hours to retrieve available updates and commit changes to this repository.

View the data in the [Evergreen Workbench](https://eucpilots.com/workbench).

## Application update runners

[scripts/SkipApps.txt](scripts/SkipApps.txt) lists the Evergreen application names handled by the self-hosted runner, one name per line. Both update scripts read this shared file on each execution by default: [Update-Json.ps1](scripts/Update-Json.ps1) skips these applications, while [Update-JsonHosted.ps1](scripts/Update-JsonHosted.ps1) updates only these applications. The file is resolved relative to the scripts, regardless of the current working directory; a missing or unreadable file stops execution.

Edit this file to change the runner assignment. The existing `-SkipApps` and `-Apps` parameters remain available to override the list for an individual execution.
