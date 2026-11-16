# VAOS Project Bundles

`.vaosproject` is a compact, configuration-only archive intended to share compatible VAOS engine settings and references between installations.

## What it contains

- A versioned `manifest.json` with producer version and SHA-256 checksum.
- A strict `project.json` with a project name/description, allowlisted engine settings, and names/IDs/counts for locally registered dataset and model references.

Dataset/model references do not carry source paths or files. Imported bundles do not download/select datasets or models, alter `best.pt`, start training, or enable rules. Applying a saved bundle changes only the engine settings shown in the review screen.

## Excluded information

Bundles never include admin passwords, API/MCP tokens or approvals, camera/enrollment/tunnel capability URLs, database contents, face templates/profile photos, review images, model weights, dataset files, logs, or executable scripts/plugins. These exclusions preserve privacy and keep bundles small.

## Archive safety

- Only `manifest.json` and `project.json` are accepted; all other entries, duplicates, case-colliding names, paths, directories, symbolic links, encryption and unsupported compression are rejected.
- Maximum compressed upload size: 2 MiB. Maximum member size: 1 MiB. Maximum uncompressed total: 2 MiB. Compression ratios above 80:1 are rejected.
- Content is streamed with a strict output limit and validated in memory; VAOS does not extract bundle members.
- JSON uses an allowlist: unknown fields such as tokens, arbitrary paths, executable commands and image data cause rejection.
- The manifest checksum must match the exact project JSON bytes. Corrupt archives and unsupported format versions are rejected.
- Import preview is dry-run only. A second explicit action saves the validated archive. Applying settings requires a separate confirmation. Bundle deletion removes only that saved archive, not referenced data.

## REST and MCP

See [the API reference](API.md#project-bundles). The `projects` capability group is required. MCP import and apply actions are sensitive writes subject to the existing exact-request approval; deletes also require the deletion setting.

## Current limitations

This is not a full backup or environment migration. It does not package Vision App templates, custom rules, models, or datasets. References must exist locally to be useful; VAOS reports missing references and leaves them for the administrator to resolve. Round-trip and hostile-archive tests are authored but were not run in the current resource-constrained session.
