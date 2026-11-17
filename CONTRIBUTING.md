# Contributing to VAOS

Thank you for helping improve VAOS. Contributions from people with different backgrounds are welcome: code, design, documentation, accessibility, translation, testing and clear bug reports all help.

## Before you contribute

- Read the [README](README.md), [project overview](docs/PROJECT_OVERVIEW.md), [security notes](docs/TESTING.md) and [AGPL-3.0-only license](LICENSE).
- Search open issues and discussions before starting a large change. For substantial changes, describe the user problem and proposed behavior first.
- Do not include passwords, API tokens, camera/tunnel links, face/profile images, private review samples, local database files, datasets or model weights in issues or pull requests.
- Keep existing copyright and license notices. Do not claim ownership over contributions made by others; the project license applies under its stated terms.

## Development and checks

Use Python 3.12 and follow the setup in the README. Keep model downloads, cameras, GPU work and user data out of unit tests. Tests should use synthetic fixtures or temporary directories and should not access a developer's VAOS data folder.

Before opening a pull request, run the relevant syntax and unit checks from [Testing and validation](docs/TESTING.md). If a check cannot run, say so and include the reason. For UI changes, include a screenshot at desktop and mobile sizes when practical, plus keyboard and empty/error-state notes.

## Pull requests

1. Explain the user need and the visible behavior change.
2. Keep changes focused and reuse existing VAOS routes, detector and permission checks.
3. Include tests for changed behavior and update the user/developer documentation.
4. List migrations, resource usage, privacy effects, known limitations and rollback steps.
5. Do not claim hardware benchmarks, safety outcomes or test passes without recorded evidence.

## Community acknowledgement

VAOS was originally created by Prateek Dubey and is developed with contributions from the open-source community. Contributors should be credited in the relevant release notes when their work is included. Creator attribution does not erase contributor credit or third-party notices.
