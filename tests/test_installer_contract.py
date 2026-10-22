import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_token_tracker.ps1"
REQUIREMENTS = ROOT / "requirements-token-tracker.txt"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


def _powershell(script):
    if not POWERSHELL:
        pytest.skip("PowerShell is required for installer contract checks")
    wrapped_script = f"try {{ {script} }} catch {{ [Console]::Error.WriteLine(($_ | Format-List * -Force | Out-String)); exit 1 }}"
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", wrapped_script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_installer_scriptblock_parses_without_running_installer_body():
    _powershell(
        "$tokens=$null; $errors=$null; "
        "[System.Management.Automation.Language.Parser]::ParseFile(" 
        f"'{INSTALLER.as_posix()}',[ref]$tokens,[ref]$errors) | Out-Null; "
        "if ($errors.Count) { $errors | ForEach-Object { Write-Error $_ }; exit 1 }"
    )


def test_installer_ownership_predicates_reject_task_and_shortcut_collisions():
    source_path = INSTALLER.as_posix().replace("'", "''")
    script = f"""
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile('{source_path}',[ref]$tokens,[ref]$errors)
$functions=$ast.FindAll({{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] }},$true)
foreach ($function in $functions) {{ Invoke-Expression $function.Extent.Text }}
$taskName='Token Tracker'
$ExpectedExe='C:\\Tracker\\.venv\\Scripts\\pythonw.exe'
$ExpectedWork='C:\\Tracker'
$ExpectedArgs='-m token_tracker.widget --background'
$ExpectedSid='S-1-5-21-1'
    $MockTask=$null
    $MockTaskError=$null
function Get-ScheduledTask {{
  if ($MockTaskError) {{ throw $MockTaskError }}
  return $MockTask
}}
$expectedExe='C:\\Tracker\\.venv\\Scripts\\pythonw.exe'
$expectedWork='C:\\Tracker'
$task=[pscustomobject]@{{ Actions=@([pscustomobject]@{{ Execute=$expectedExe; Arguments='-m token_tracker.widget --background'; WorkingDirectory=$expectedWork }}); Principal=[pscustomobject]@{{ UserId='S-1-5-21-1'; LogonType='Interactive'; RunLevel='Limited' }} }}
if (-not (Test-TokenTrackerTaskOwnership $task $expectedExe '-m token_tracker.widget --background' $expectedWork 'S-1-5-21-1')) {{ throw 'Expected owned task did not match' }}
$negative=@(
  [pscustomobject]@{{ Actions=@([pscustomobject]@{{ Execute='C:\\Other\\pythonw.exe'; Arguments='-m token_tracker.widget --background'; WorkingDirectory=$expectedWork }}); Principal=$task.Principal }},
  [pscustomobject]@{{ Actions=@([pscustomobject]@{{ Execute=$expectedExe; Arguments='-m unrelated'; WorkingDirectory=$expectedWork }}); Principal=$task.Principal }},
  [pscustomobject]@{{ Actions=@([pscustomobject]@{{ Execute=$expectedExe; Arguments='-m token_tracker.widget --background'; WorkingDirectory='C:\\Other' }}); Principal=$task.Principal }},
  [pscustomobject]@{{ Actions=@($task.Actions[0],$task.Actions[0]); Principal=$task.Principal }},
  [pscustomobject]@{{ Actions=$task.Actions; Principal=[pscustomobject]@{{ UserId='S-1-5-21-2'; LogonType='Interactive'; RunLevel='Limited' }} }},
  [pscustomobject]@{{ Actions=$task.Actions; Principal=[pscustomobject]@{{ UserId='S-1-5-21-1'; LogonType='Password'; RunLevel='Limited' }} }},
  [pscustomobject]@{{ Actions=$task.Actions; Principal=[pscustomobject]@{{ UserId='S-1-5-21-1'; LogonType='Interactive'; RunLevel='Highest' }} }},
  [pscustomobject]@{{ Actions=@([pscustomobject]@{{ Execute=$expectedExe; Arguments='-m token_tracker.widget --background' }}); Principal=$task.Principal }}
)
foreach ($candidate in $negative) {{ if (Test-TokenTrackerTaskOwnership $candidate $expectedExe '-m token_tracker.widget --background' $expectedWork 'S-1-5-21-1') {{ throw 'Unowned task was accepted' }} }}
$shortcut=[pscustomobject]@{{ TargetPath=$expectedExe; Arguments='-m token_tracker.widget'; WorkingDirectory=$expectedWork }}
if (-not (Test-TokenTrackerShortcutOwnership $shortcut $expectedExe '-m token_tracker.widget' $expectedWork)) {{ throw 'Expected owned shortcut did not match' }}
$badShortcuts=@(
  [pscustomobject]@{{ TargetPath='C:\\Other\\pythonw.exe'; Arguments='-m token_tracker.widget'; WorkingDirectory=$expectedWork }},
  [pscustomobject]@{{ TargetPath=$expectedExe; Arguments='-m unrelated'; WorkingDirectory=$expectedWork }},
  [pscustomobject]@{{ TargetPath=$expectedExe; Arguments='-m token_tracker.widget'; WorkingDirectory='C:\\Other' }},
  [pscustomobject]@{{ TargetPath=$expectedExe; Arguments='-m token_tracker.widget' }}
)
foreach ($candidate in $badShortcuts) {{ if (Test-TokenTrackerShortcutOwnership $candidate $expectedExe '-m token_tracker.widget' $expectedWork) {{ throw 'Unowned shortcut was accepted' }} }}
$MockTaskError="No MSFT_ScheduledTask objects found with property 'TaskName' equal to 'Token Tracker'."
if ((Get-TokenTrackerTaskState $ExpectedExe $ExpectedArgs $ExpectedWork $ExpectedSid) -ne 'Absent') {{ throw 'Missing task was not treated as absent' }}
$MockTaskError=$null
$MockTask=$null
try {{ [void](Get-TokenTrackerTaskState $ExpectedExe $ExpectedArgs $ExpectedWork $ExpectedSid); throw 'Empty task metadata was accepted as absence' }} catch {{ if ($_.Exception.Message -notmatch 'Could not safely inspect') {{ throw }} }}
$MockTask=$task
if ((Get-TokenTrackerTaskState $ExpectedExe $ExpectedArgs $ExpectedWork $ExpectedSid) -ne 'Owned') {{ throw 'Owned task state was not recognized' }}
$MockTask=$negative[0]
try {{ [void](Get-TokenTrackerTaskState $ExpectedExe $ExpectedArgs $ExpectedWork $ExpectedSid); throw 'Unrelated task was accepted' }} catch {{ if ($_.Exception.Message -notmatch 'refusing to overwrite') {{ throw }} }}
$MockTask=@($task,$task)
try {{ [void](Get-TokenTrackerTaskState $ExpectedExe $ExpectedArgs $ExpectedWork $ExpectedSid); throw 'Ambiguous task was accepted' }} catch {{ if ($_.Exception.Message -notmatch 'refusing to overwrite') {{ throw }} }}
$MockTask=$null
$MockTaskError='Access denied while reading task'
try {{ [void](Get-TokenTrackerTaskState $ExpectedExe $ExpectedArgs $ExpectedWork $ExpectedSid); throw 'Unreadable task was accepted' }} catch {{ if ($_.Exception.Message -notmatch 'Could not safely inspect') {{ throw }} }}
$global:MockShortcut=$shortcut
$shell=New-Object psobject
$shell | Add-Member ScriptMethod CreateShortcut {{ param($path) if ($global:MockShortcutError) {{ throw $global:MockShortcutError }}; return $global:MockShortcut }}
$existingPath='{source_path}'
if ((Get-TokenTrackerShortcutState $existingPath $expectedExe '-m token_tracker.widget' $expectedWork) -ne 'Owned') {{ throw 'Owned shortcut state was not recognized' }}
$global:MockShortcut=$null
try {{ [void](Get-TokenTrackerShortcutState $existingPath $expectedExe '-m token_tracker.widget' $expectedWork); throw 'Missing shortcut metadata was accepted' }} catch {{ if ($_.Exception.Message -notmatch 'Could not safely inspect') {{ throw }} }}
$global:MockShortcut=$badShortcuts[0]
try {{ [void](Get-TokenTrackerShortcutState $existingPath $expectedExe '-m token_tracker.widget' $expectedWork); throw 'Unrelated shortcut was accepted' }} catch {{ if ($_.Exception.Message -notmatch 'refusing to overwrite') {{ throw }} }}
$global:MockShortcutError='Shortcut read failed'
try {{ [void](Get-TokenTrackerShortcutState $existingPath $expectedExe '-m token_tracker.widget' $expectedWork); throw 'Unreadable shortcut was accepted' }} catch {{ if ($_.Exception.Message -notmatch 'Could not safely inspect') {{ throw }} }}
Write-Output 'mocked ownership checks passed'
"""
    _powershell(script)


def test_collision_checks_precede_pip_and_destinations_are_rechecked_before_writes():
    source = INSTALLER.read_text(encoding="utf-8")
    initial_checks = source.index("$shortcutState = Get-TokenTrackerShortcutState", source.index("# Startup collisions"))
    initial_task_check = source.index("$taskState = Get-TokenTrackerTaskState", initial_checks)
    first_environment_write = source.index("-m venv $environmentRoot")
    first_pip = source.index("-m pip install $desktopRequirements")
    assert initial_checks < first_environment_write < first_pip
    assert initial_task_check < first_environment_write
    assert source.rindex("Get-TokenTrackerShortcutState") < source.index("$shortcut.Save()")
    assert source.rindex("Get-TokenTrackerTaskState") < source.index("Register-ScheduledTask")
    assert "if ($taskState -eq 'Owned') { $registerArguments.Force = $true }" in source
    assert "Register-ScheduledTask @registerArguments" in source
    assert "Register-ScheduledTask -TaskName $taskName -Action $taskAction" not in source


def test_all_task_state_call_sites_match_declared_parameter_names():
    source_path = INSTALLER.as_posix().replace("'", "''")
    script = f"""
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile('{source_path}',[ref]$tokens,[ref]$errors)
if ($errors.Count) {{ throw 'Installer script did not parse' }}
$definition=$ast.Find({{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-TokenTrackerTaskState' }},$true)
$declared=@($definition.Body.ParamBlock.Parameters | ForEach-Object {{ $_.Name.VariablePath.UserPath.TrimStart([char[]]'$') }})
$calls=@($ast.FindAll({{ param($node) $node -is [System.Management.Automation.Language.CommandAst] -and $node.GetCommandName() -eq 'Get-TokenTrackerTaskState' }},$true))
if ($calls.Count -ne 2) {{ throw "Expected two task-state calls; found $($calls.Count)" }}
foreach ($call in $calls) {{
  $named=@($call.CommandElements | Where-Object {{ $_ -is [System.Management.Automation.Language.CommandParameterAst] }} | ForEach-Object {{ $_.ParameterName }})
  if (($named | Where-Object {{ $_ -notin $declared }}).Count) {{ throw "Invalid named parameter at task-state call: $($call.Extent.Text)" }}
  if (($declared | Where-Object {{ $_ -notin $named }}).Count) {{ throw "Missing named parameter at task-state call: $($call.Extent.Text)" }}
}}
$functions=$ast.FindAll({{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] }},$true)
foreach ($function in $functions) {{ Invoke-Expression $function.Extent.Text }}
$taskName='Token Tracker'
$pythonw='C:\\Tracker\\.venv\\Scripts\\pythonw.exe'
$expectedTaskArguments='-m token_tracker.widget --background'
$repoRoot='C:\\Tracker'
$userSid='S-1-5-21-1'
$MockTask=[pscustomobject]@{{ Actions=@([pscustomobject]@{{ Execute=$pythonw; Arguments=$expectedTaskArguments; WorkingDirectory=$repoRoot }}); Principal=[pscustomobject]@{{ UserId=$userSid; LogonType='Interactive'; RunLevel='Limited' }} }}
function Get-ScheduledTask {{ return $MockTask }}
foreach ($call in $calls) {{
  $result=Invoke-Expression $call.Extent.Text
  if ($result -ne 'Owned') {{ throw "Same-install task recheck failed: $($call.Extent.Text) => $result" }}
}}
$MockTask.Actions[0].Execute='C:\\Other\\pythonw.exe'
foreach ($call in $calls) {{
  try {{ [void](Invoke-Expression $call.Extent.Text); throw "Mismatched task target passed recheck: $($call.Extent.Text)" }} catch {{ if ($_.Exception.Message -notmatch 'refusing to overwrite') {{ throw }} }}
}}
Write-Output 'task-state call parameters passed'
"""
    _powershell(script)


def test_installer_reconciles_pinned_desktop_requirements_before_import_check():
    source = INSTALLER.read_text(encoding="utf-8")
    requirements = REQUIREMENTS.read_text(encoding="utf-8")
    pip_install = source.index("-m pip install $desktopRequirements")
    import_check = source.index('import webview, pystray, PIL, zstandard', source.index("-m pip install $desktopRequirements"))
    assert pip_install < import_check
    assert "pywebview==6.2.1" in requirements
    assert "pystray==0.19.5" in requirements
    assert "Pillow==12.3.0" in requirements
    assert "zstandard==0.25.0" in requirements
    assert "pywebpush==2.5.0" in requirements.split("# Phone Web Push", 1)[1]
    assert "pywebpush" not in source.split("# Phone Web Push", 1)[0]
