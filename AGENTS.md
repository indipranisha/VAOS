# VAOS repository instructions

## Versioning

The owner froze the source baseline as **1.2.0** on 2026-11-24.
After every completed user-requested change to source, UI, configuration, tests,
or documentation, run `python3 bump_version.py` once before final verification.
This automatically advances the patch version within 1.2.x and synchronizes
runtime, package, citation, CLI, telemetry, fallback, and README version fields.
Treat one coherent user request as one change, not each file edit or test run.
Read-only questions, inspection, or operating the running app do not bump the
source version. Do not bump again merely to fix verification failures in the
same change. Honor any later explicit version instruction from the owner.
Historical release notes and validation records retain their original versions.
Mention the new version in the final response. A source version change does not
mean that already-running services have reloaded it. Do not restart active jobs
or create a Git release/tag merely to update a version.

The owner authorized the 1.3.0 beta line for Daksh-AI. Increment `1.3.0-beta.N` once per coherent completed change using bump_version.py. Keep historical validation records unchanged.

The owner explicitly selected **1.3.1-beta1** for engine controls, historical adviser learning and the Detection workspace on 2026-11-26. Future coherent changes may increment betaN; explicit owner versions take precedence.

The owner authorized **1.3.3 stable** on 2026-11-26, superseding the beta version policy. Subsequent coherent changes increment the current stable patch (1.3.x) unless the owner specifies otherwise.
