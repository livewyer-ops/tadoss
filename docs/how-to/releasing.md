# Releasing

Edit `src/app/tadoss/release.json` and the operator catalogue together. Retain supported catalogue entries with their original immutable image references. Set the new entry's `upgradeFrom` to the predecessor releases qualified for that transition; the first release uses an empty list. Artifact generation updates only the current entry's runtime digest and preserves the other entries. Runtime package version is read from the release file. Regenerate and review the contract when changing its pinned upstream revision. Run `task release:check`, `task check`, `task security:audit` and deployed qualification before tagging.

The release workflow checks the tag, builds and scans the multi-architecture runtime image, writes its exact digest into the operator catalogue, then builds the operator image. It qualifies those digest references, assembles install YAML and compatibility metadata, and signs the images and checksum file. SBOM and provenance are attached to image builds. Release assets include the catalogue, install manifest, compatibility matrix and checksums.

Tags use `v<version>`, matching the runtime release metadata exactly. A version with a prerelease suffix, such as `0.1.0-rc1`, is published as a GitHub prerelease.

Do not replace an existing version. Failed candidate images may remain under commit-specific candidate tags; version tags and GitHub release assets are published only after qualification. Updating a release requires a new version and tag.

Local artifact generation requires an operator built with the same runtime digest in its embedded catalogue. The artifact command refuses a catalogue mismatch or a nonempty output directory:

```sh
task release:artifacts RUNTIME_IMAGE=registry.example/api@sha256:<digest> OPERATOR_IMAGE=registry.example/operator@sha256:<digest>
```

Migration histories may be consolidated before the first public release. After publication, keep released migrations unchanged and append new migrations. Database migrations support the current ordered schema and reject incompatible migration histories. See the [changelog](../../CHANGELOG.md) for database reset and migration prerequisites.

Qualification must run every profile against the same final runtime and operator builds, including a scheduled backup completing after Retain deletion. Native ARM64 remains a required CI check. Record local architecture, image identities and any unexecuted checks in the qualification report; a local AMD64 pass does not stand in for ARM64. Install released manifests with the [defaults overlay or explicit instance fields](../../deploy/README.md#install-a-digest-pinned-release).
