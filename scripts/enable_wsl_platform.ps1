# Run only after the user approves this system feature change.
# This script never restarts Windows.
$ErrorActionPreference = 'Stop'
$taskIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$taskPrincipal = [Security.Principal.WindowsPrincipal]::new($taskIdentity)
if (-not $taskPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Administrator PowerShell is required to enable VirtualMachinePlatform.'
}
$taskFeature = Get-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform
if ($taskFeature.State -eq 'Enabled') {
    Write-Output 'VirtualMachinePlatform is already enabled; no changes made.'
    return
}
$taskResult = Enable-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform -All -NoRestart
$taskResult | Select-Object FeatureName,State,RestartNeeded
Write-Output 'No restart was initiated. Save your work and restart Windows when ready.'
