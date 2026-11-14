# Install

## Windows

Requirements: 64-bit Windows 10 (build 19041) or Windows 11, with Microsoft Edge WebView2 Runtime and .NET Framework 4.8 (both ship with current Windows). Codex must be signed in for Codex chats and limits. Claude Code is optional.

1. Download `BranchglanceSetup.exe` from the [releases page](https://github.com/apratxxm/branchglance/releases). Releases before 1.0 are published as pre-releases.
2. Run it from File Explorer as your normal user, not as administrator.
3. Open **Branchglance** from the Start menu.

Setup installs per user to `%LOCALAPPDATA%\Programs\Branchglance`, adds Start menu and Desktop shortcuts, and can start the widget at sign-in if you choose. It checks its prerequisites and stops with an explanation if one is missing.

### Verify the download

Compare the SHA-256 hash with the `.sha256` file published next to the installer:

```powershell
Get-FileHash .\BranchglanceSetup.exe -Algorithm SHA256
```

Builds that are not yet code-signed may trigger a SmartScreen warning on first run. For signed builds, also check the signature (see the [code signing policy](CODE_SIGNING_POLICY.md)):

```powershell
Get-AuthenticodeSignature .\BranchglanceSetup.exe | Format-List Status, SignerCertificate
```

### Update, roll back, uninstall

- **Update:** run the newer Setup. It installs the new release beside the old one, self-tests it, then switches over.
- **Roll back:** `BranchglanceSetup.exe --rollback` restores the previous verified release.
- **Uninstall:** Settings → Apps → Installed apps → Branchglance. It revokes Claude consent, backs up Claude settings and removes only Branchglance's own hooks, shortcuts and program files. Your settings and history in `%LOCALAPPDATA%\Branchglance` are kept.

## macOS (from source)

Requires macOS 14 or later, Python 3.12+ and the Xcode command line tools. Then:

The build checks that the Python interpreter it will use is version 3.12 or newer.

```sh
git clone https://github.com/apratxxm/branchglance.git
cd branchglance
./macos/build.sh install
```

This installs the notch app in `/Applications`. Rollback, uninstall (`./macos/build.sh uninstall --purge` also deletes saved data) and signing are covered in the [macOS guide](../macos/README.md). Builds without a Developer ID are signed locally and run on the machine that built them. There is no macOS installer download yet.

## Troubleshooting

- **Setup won't continue:** check the requirements above and run Setup from File Explorer, not from another app's terminal or as administrator.
- **Setup refuses an existing folder:** it won't overwrite files it can't identify as its own. Move the folder aside instead of deleting files to force it.
- **No Claude chats:** turn on Claude tracking in Settings → Notifications. Only sessions that report hook events can appear.
- **A Codex job shows as its own parent:** its link to a Claude chat couldn't be proven. See [why a job shows up as its own parent](INTEGRATION.md#why-a-job-shows-up-as-its-own-parent).
- **Limits show an age such as "13m ago":** the provider hasn't reported a fresh value yet. Claude limits also refresh from [live usage](CONFIGURATION.md#claude-live-usage) and the optional status line helper.
