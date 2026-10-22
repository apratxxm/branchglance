param(
    [string]$PythonPath,
    [switch]$StartAtSignIn
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$environmentRoot = Join-Path $repoRoot ".venv"
$python = Join-Path $environmentRoot "Scripts\python.exe"
$pythonw = Join-Path $environmentRoot "Scripts\pythonw.exe"
$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktop "Token Tracker.lnk"
$taskName = "Token Tracker"
$expectedTaskArguments = "-m token_tracker.widget --background"
$expectedShortcutArguments = "-m token_tracker.widget"

function Normalize-InstallerPath {
    param([string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        return ""
    }
    $expanded = [Environment]::ExpandEnvironmentVariables($Path.Trim())
    try {
        $fullPath = [IO.Path]::GetFullPath($expanded)
    } catch {
        return ""
    }
    $root = [IO.Path]::GetPathRoot($fullPath)
    if ($fullPath.Length -gt $root.Length) {
        $fullPath = $fullPath.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    }
    return $fullPath
}

function Test-TokenTrackerShortcutOwnership {
    param(
        [Parameter(Mandatory = $true)]$Shortcut,
        [Parameter(Mandatory = $true)][string]$ExpectedPythonw,
        [Parameter(Mandatory = $true)][string]$ExpectedArguments,
        [Parameter(Mandatory = $true)][string]$ExpectedWorkingDirectory
    )

    return ((Normalize-InstallerPath $Shortcut.TargetPath) -ieq (Normalize-InstallerPath $ExpectedPythonw)) -and
        ($Shortcut.Arguments -ceq $ExpectedArguments) -and
        ((Normalize-InstallerPath $Shortcut.WorkingDirectory) -ieq (Normalize-InstallerPath $ExpectedWorkingDirectory))
}

function Test-TokenTrackerTaskOwnership {
    param(
        [Parameter(Mandatory = $true)]$Task,
        [Parameter(Mandatory = $true)][string]$ExpectedPythonw,
        [Parameter(Mandatory = $true)][string]$ExpectedArguments,
        [Parameter(Mandatory = $true)][string]$ExpectedWorkingDirectory,
        [Parameter(Mandatory = $true)][string]$ExpectedUserSid
    )

    try {
        if (@($Task.Actions).Count -ne 1) { return $false }
        $action = @($Task.Actions)[0]
        if ((Normalize-InstallerPath $action.Execute) -ine (Normalize-InstallerPath $ExpectedPythonw)) { return $false }
        if ($action.Arguments -cne $ExpectedArguments) { return $false }
        if ((Normalize-InstallerPath $action.WorkingDirectory) -ine (Normalize-InstallerPath $ExpectedWorkingDirectory)) { return $false }
        $principalSid = $null
        if ($Task.Principal.UserId -match '^S-1-') {
            $principalSid = $Task.Principal.UserId
        } else {
            $principalSid = ([Security.Principal.NTAccount]$Task.Principal.UserId).Translate([Security.Principal.SecurityIdentifier]).Value
        }
        if ($principalSid -cne $ExpectedUserSid) { return $false }
        if ($Task.Principal.LogonType.ToString() -cne 'Interactive') { return $false }
        if ($Task.Principal.RunLevel.ToString() -cne 'Limited') { return $false }
        return $true
    } catch {
        return $false
    }
}

function Get-TokenTrackerTaskState {
    param([string]$ExpectedPythonw, [string]$ExpectedArguments, [string]$ExpectedWorkingDirectory, [string]$ExpectedUserSid)

    try {
        $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
    } catch {
        if ($_.FullyQualifiedErrorId -like 'No MSFT_ScheduledTask objects found*' -or $_.Exception.Message -match "^No MSFT_ScheduledTask objects found with property 'TaskName' equal to") {
            return 'Absent'
        }
        throw "Could not safely inspect scheduled task '$taskName': $($_.Exception.Message)"
    }
    if (-not $existingTask) {
        throw "Could not safely inspect scheduled task '$taskName': the task query returned no metadata."
    }
    if (Test-TokenTrackerTaskOwnership -Task $existingTask -ExpectedPythonw $ExpectedPythonw -ExpectedArguments $ExpectedArguments -ExpectedWorkingDirectory $ExpectedWorkingDirectory -ExpectedUserSid $ExpectedUserSid) {
        return 'Owned'
    }
    throw "Scheduled task '$taskName' already exists but does not match this Token Tracker installation; refusing to overwrite it."
}

function Get-TokenTrackerShortcutState {
    param([string]$Path, [string]$ExpectedPythonw, [string]$ExpectedArguments, [string]$ExpectedWorkingDirectory)

    if (-not (Test-Path -LiteralPath $Path)) { return 'Absent' }
    try {
        $probe = $shell.CreateShortcut($Path)
        $matches = Test-TokenTrackerShortcutOwnership -Shortcut $probe -ExpectedPythonw $ExpectedPythonw -ExpectedArguments $ExpectedArguments -ExpectedWorkingDirectory $ExpectedWorkingDirectory
        if ([Runtime.InteropServices.Marshal]::IsComObject($probe)) {
            [void][Runtime.InteropServices.Marshal]::ReleaseComObject($probe)
        }
    } catch {
        throw "Could not safely inspect desktop shortcut '$Path': $($_.Exception.Message)"
    }
    if ($matches) { return 'Owned' }
    throw "Desktop shortcut '$Path' already exists but does not match this Token Tracker installation; refusing to overwrite it."
}

# Startup collisions are checked before environment creation, dependency installs, or either destination write.
$shell = New-Object -ComObject WScript.Shell
$userSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$shortcutState = Get-TokenTrackerShortcutState -Path $shortcutPath -ExpectedPythonw $pythonw -ExpectedArguments $expectedShortcutArguments -ExpectedWorkingDirectory $repoRoot
$taskState = 'NotRequested'
if ($StartAtSignIn) {
    $taskState = Get-TokenTrackerTaskState -ExpectedPythonw $pythonw -ExpectedArguments $expectedTaskArguments -ExpectedWorkingDirectory $repoRoot -ExpectedUserSid $userSid
}

function Get-SupportedPythonVersion {
    param([string]$Executable, [string[]]$PrefixArguments = @())

    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
        return $null
    }
    $versionOutput = & $Executable @PrefixArguments --version 2>&1
    if ($LASTEXITCODE -ne 0) {
        return $null
    }
    $match = [regex]::Match(($versionOutput | Out-String), 'Python\s+(\d+)\.(\d+)')
    if (-not $match.Success) {
        return $null
    }
    $major = [int]$match.Groups[1].Value
    $minor = [int]$match.Groups[2].Value
    if (($major -lt 3) -or ($major -eq 3 -and $minor -lt 10)) {
        return $null
    }
    return "$major.$minor"
}

if (Test-Path -LiteralPath $python -PathType Leaf) {
    $pythonVersion = Get-SupportedPythonVersion -Executable $python
    if (-not $pythonVersion) {
        throw "The existing environment uses unsupported or broken Python at $python. Repair or remove that environment, then rerun setup. Python 3.10 or newer is required."
    }
} else {
    $basePython = $null
    $baseArguments = @()

    if ($PythonPath) {
        $resolvedPython = (Resolve-Path -LiteralPath $PythonPath -ErrorAction Stop).Path
        $pythonVersion = Get-SupportedPythonVersion -Executable $resolvedPython
        if (-not $pythonVersion) {
            throw "-PythonPath must point to a working Python 3.10 or newer executable: $resolvedPython"
        }
        $basePython = $resolvedPython
    } else {
        $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
        if ($pyLauncher) {
            $pythonVersion = Get-SupportedPythonVersion -Executable $pyLauncher.Source -PrefixArguments @("-3")
            if ($pythonVersion) {
                $basePython = $pyLauncher.Source
                $baseArguments = @("-3")
            }
        }
        if (-not $basePython) {
            $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
            if ($pythonCommand) {
                $pythonVersion = Get-SupportedPythonVersion -Executable $pythonCommand.Source
                if ($pythonVersion) {
                    $basePython = $pythonCommand.Source
                }
            }
        }
        if (-not $basePython) {
            throw "Python 3.10 or newer was not found. Install Python and enable the py launcher or python on PATH, or rerun with -PythonPath <python.exe>."
        }
    }

    $venvOutput = & $basePython @baseArguments -m venv $environmentRoot 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create the local Python environment with $basePython. $($venvOutput | Out-String)"
    }
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "Python reported success creating the environment, but its interpreter is missing at $python."
    }
    $pythonVersion = Get-SupportedPythonVersion -Executable $python
    if (-not $pythonVersion) {
        throw "The new environment does not contain a working Python 3.10 or newer interpreter at $python."
    }
}

if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) {
    throw "The Python environment is missing pythonw.exe at $pythonw. Install a standard Windows Python distribution and rerun setup."
}

$requirementsPath = Join-Path $repoRoot "requirements-token-tracker.txt"
if (-not (Test-Path -LiteralPath $requirementsPath -PathType Leaf)) {
    throw "Desktop dependency list was not found: $requirementsPath"
}
$desktopRequirements = @()
$foundPhoneMarker = $false
foreach ($line in Get-Content -LiteralPath $requirementsPath) {
    if ($line.Trim() -eq "# Phone Web Push") {
        $foundPhoneMarker = $true
        break
    }
    $requirement = $line.Trim()
    if ($requirement -and -not $requirement.StartsWith("#")) {
        $desktopRequirements += $requirement
    }
}
if (-not $foundPhoneMarker) {
    throw "Could not find the '# Phone Web Push' boundary in $requirementsPath; refusing to install an unreviewed dependency list."
}
if ($desktopRequirements.Count -eq 0) {
    throw "No desktop dependencies were found before the phone dependency boundary in $requirementsPath."
}

& $python -m pip --version 2>$null
if ($LASTEXITCODE -ne 0) {
    & $python -m ensurepip --upgrade
    if ($LASTEXITCODE -ne 0) {
        throw "Could not bootstrap pip in the local Python environment."
    }
}
& $python -m pip install $desktopRequirements
if ($LASTEXITCODE -ne 0) {
    throw "Could not install the desktop dependencies from $requirementsPath."
}
& $python -c "import webview, pystray, PIL, zstandard"
if ($LASTEXITCODE -ne 0) {
    throw "Desktop dependency installation completed, but the required imports still fail."
}

$codexExecutable = $null
$codexCommand = Get-Command codex -ErrorAction SilentlyContinue
if ($codexCommand) {
    $codexExecutable = $codexCommand.Source
} else {
    $localAppData = $env:LOCALAPPDATA
    $codexCandidates = @()
    if ($localAppData) {
        $codexCandidates += Join-Path $localAppData "Programs\OpenAI\Codex\bin\codex.exe"
        $codexCandidates += Join-Path $localAppData "Programs\codex\bin\codex.exe"
    }
    if ($env:USERPROFILE) {
        $codexCandidates += Join-Path $env:USERPROFILE "AppData\Local\Programs\OpenAI\Codex\bin\codex.exe"
    }
    foreach ($candidate in $codexCandidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $codexExecutable = $candidate
            break
        }
    }
}
if (-not $codexExecutable) {
    Write-Warning "Codex CLI/app-server was not found. The desktop widget can open, but live Codex account data and quota features may be unavailable. Install or expose the Codex CLI, then restart the tracker."
}

$shell = New-Object -ComObject WScript.Shell
$shortcutState = Get-TokenTrackerShortcutState -Path $shortcutPath -ExpectedPythonw $pythonw -ExpectedArguments $expectedShortcutArguments -ExpectedWorkingDirectory $repoRoot
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = "-m token_tracker.widget"
$shortcut.WorkingDirectory = $repoRoot
$shortcut.Description = "Open the live Codex token tracker"
$shortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll,44"
$shortcutState = Get-TokenTrackerShortcutState -Path $shortcutPath -ExpectedPythonw $pythonw -ExpectedArguments $expectedShortcutArguments -ExpectedWorkingDirectory $repoRoot
$shortcut.Save()

Write-Output "Installed current-user desktop shortcut: $shortcutPath"
if ($StartAtSignIn) {
    $taskAction = New-ScheduledTaskAction -Execute $pythonw -Argument $expectedTaskArguments -WorkingDirectory $repoRoot
    $taskTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $taskSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    $registerArguments = @{
        TaskName = $taskName
        Action = $taskAction
        Trigger = $taskTrigger
        Settings = $taskSettings
        Principal = $taskPrincipal
        Description = "Keep Token Tracker and its limit-reset watcher running after sign-in"
    }
    $taskState = Get-TokenTrackerTaskState -ExpectedPythonw $pythonw -ExpectedArguments $expectedTaskArguments -ExpectedWorkingDirectory $repoRoot -ExpectedUserSid $userSid
    if ($taskState -eq 'Owned') { $registerArguments.Force = $true }
    Register-ScheduledTask @registerArguments | Out-Null
    Write-Output "Installed sign-in task: $taskName"
}

