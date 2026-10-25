# SignPath Foundation setup

This repository prepares the build, signing configuration, and release policy. A maintainer must complete the external SignPath setup; no credential belongs in the repository.

## 1. Apply to SignPath Foundation

1. Apply at [signpath.org](https://signpath.org/) for the SignPath Foundation free code-signing program.
2. Identify the public Branchglance GitHub repository and provide the checked-in [code signing policy](../CODE_SIGNING_POLICY.md).
3. Wait for approval and complete the project setup SignPath requests. Do not claim signed status or publish a signed release before approval.

### Confirm the bundled WebView2 components are eligible

The current PyInstaller bundle includes `Microsoft.Web.WebView2.Core.dll` and `Microsoft.Web.WebView2.WinForms.dll` from pywebview's Windows package. These are upstream Microsoft WebView2 interop assemblies, not Branchglance code. SignPath Foundation's terms permit unsigned upstream OSS binaries in a signed package, but exclude proprietary components except system libraries. Ask SignPath to confirm that these redistributable WebView2 assemblies are eligible before setting the signing variables. If they are not, resolve the dependency/licensing issue before requesting signatures; this workflow deliberately does not sign third-party DLLs.

## 2. Secure the maintainer accounts

Enable multi-factor authentication on the GitHub accounts and SignPath accounts used as committers, reviewers, submitters, and approvers. The current policy assigns all roles to `apratxxm`; add reviewers and approvers as the team grows.

## 3. Install the SignPath GitHub App

In SignPath, connect the GitHub.com trusted build system and install the official SignPath GitHub App for the Branchglance repository. Grant access only to that repository. Ensure the project and release signing policy accept GitHub Actions artifacts from the tagged release build.

Create a SignPath project for **Branchglance** and configure its artifact configuration using `.signpath/artifact-configuration.xml`. The configuration signs the launcher, app, hook, uninstaller, or final setup executable when present, while requiring ProductName `Branchglance` and the release ProductVersion. Configure a policy that requires manual approval for each signing request.

## 4. Add GitHub Actions settings

Under the repository's **Settings → Secrets and variables → Actions**, add these repository variables:

- `SIGNPATH_ORGANIZATION_ID`
- `SIGNPATH_PROJECT_SLUG`
- `SIGNPATH_SIGNING_POLICY_SLUG`
- `SIGNPATH_ARTIFACT_CONFIGURATION_SLUG`

Create a SignPath API token for a submitter allowed by the release policy, then store it as the repository secret `SIGNPATH_API_TOKEN`. Never put the token in a workflow file, variable, build log, or commit.

If the variables or token are absent, the signing and draft-release jobs skip; the build job still runs and retains the unsigned components as a short-lived Actions artifact.

## 5. Create the first signed release

1. Merge and review the release workflow, dependency pins, artifact configuration, and policy page.
2. Create a new version tag such as `v1.0.1` on the exact source commit to release. To dispatch manually, select that existing tag as the workflow ref and enter the same value in the `tag` input; signing is limited to tag refs.
3. The Windows build job runs Python and Node tests, builds the release, and uploads unsigned components.
4. SignPath creates a component signing request. Review its source and artifact details, then manually approve it in SignPath.
5. CI refreshes the manifest for the signed app files, packages those signed files into Setup, and submits Setup as a second signing request. Review and manually approve that request too.
6. After both requests finish, CI creates a **draft** GitHub release with the signed Setup executable, signed component archive, and SHA-256 files. Verify the release assets and Authenticode publisher/version metadata, then publish the draft yourself.

The signing action waits for each request to complete. Approve both requests while the workflow is waiting; if a request exceeds its configured wait window, inspect SignPath and rerun the workflow after confirming the request state.
