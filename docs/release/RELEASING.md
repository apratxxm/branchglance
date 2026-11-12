# Releasing

How a Branchglance release goes from a commit to a published GitHub release. The build and signing are automated by [`.github/workflows/release.yml`](../../.github/workflows/release.yml); a maintainer prepares the commit, approves signing and publishes.

## Versioning

[Semantic Versioning](https://semver.org), pre-1.0:

| Change | Bump | Example |
|---|---|---|
| New feature or visible behaviour change | minor | 0.1.0 → 0.2.0 |
| Bug fix only | patch | 0.2.0 → 0.2.1 |

Releases before 1.0 are marked **pre-release** on GitHub. Builds carry the commit after a `+`, for example `0.2.0+910b6d3`.

## 1. Prepare the release commit

1. Set `RELEASE_VERSION` in `packaging/build.py` to the new version, and update the version asserted in `tests/test_installer_build.py`.
2. In `CHANGELOG.md`, rename `[Unreleased]` to `[<version>] - <YYYY-MM-DD>` and add a fresh empty `[Unreleased]` above it. Update the compare links at the bottom.
3. Write `docs/release/RELEASE_NOTES_<version>.md`: a short highlights list, install steps, known limitations and a privacy summary. The release workflow uses this file as the release body; without it, GitHub generates notes using [`.github/release.yml`](../../.github/release.yml).
4. Run both test suites and walk through the [release checklist](RELEASE_CHECKLIST.md) on a disposable VM.
5. Merge to `master`.

## 2. Tag

```sh
git tag -a v<version> -m "Branchglance <version>"
git push origin v<version>
```

The tag must be `v` + the version in `packaging/build.py`, and the top version heading in `CHANGELOG.md` must match, or the workflow stops at its first step.

## 3. Build and sign

Pushing the tag starts the **Windows release** workflow (you can also run it manually against an existing tag). It:

1. Builds and tests the release with `packaging/build.py` and uploads the unsigned components.
2. Submits the components to SignPath. **Approve the request in SignPath** (a manual approval is required).
3. Packages the signed components into `BranchglanceSetup.exe` and submits that to SignPath. **Approve this second request too.**
4. Creates a **draft** GitHub release containing the signed Setup, the signed components archive and their `.sha256` files.

If the SignPath variables or token are not configured, signing and the draft are skipped and only an unsigned build artifact is kept. One-time SignPath setup is in [SIGNPATH_SETUP.md](SIGNPATH_SETUP.md); the policy is in the [code signing policy](../CODE_SIGNING_POLICY.md).

## 4. Publish

1. Open the draft release. Check the assets, hashes and that Setup's Authenticode publisher and version are correct.
2. Tick **Set as a pre-release** while the version is below 1.0.
3. Publish. The workflow never publishes for you.

## macOS

There is no macOS installer download yet. macOS users build from source with `./macos/build.sh install`; see the [macOS guide](../../macos/README.md).

## If something goes wrong

- **Tag/version mismatch:** delete the tag locally and on `origin`, fix the commit, and tag again. Never reuse a tag that was published.
- **Signing request rejected or timed out:** inspect it in SignPath and re-run the workflow against the same tag.
- **Draft refused because the tag moved:** the tag must still point at the built commit. Re-tag and rebuild.
