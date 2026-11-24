# Software releases and updates

VAOS checks the public indipranisha/VAOS GitHub repository for its latest published stable release at startup in a background thread. Drafts, prereleases and ordinary branch commits are excluded. Checks have a four-second network timeout and are cached in memory for one hour, including failures. GitHub receives a normal public API request, without VAOS tokens, camera data, paths or telemetry. Set VAOS_UPDATE_CHECK=0 before starting VAOS to disable checks.

A newer release displays a dismissible notice at the top of admin pages. About provides Check for updates, release notes, issue reporting and source links. Dismissal lasts for the browser session and applies only to that release. Checks compare installed source metadata rather than the version of a service left running through a source change. If offline or no stable release exists, VAOS continues normally. A 404 means no public release was found or the repository is unavailable publicly.

This first stage provides release discovery and links to update instructions. It does not install or restart anything. One-click installation and rollback are not enabled. Finish active training/import/video jobs and back up configuration before manually updating. Preserve local changes; never use a forced reset or pull over a modified working tree. Follow the published release's platform and migration instructions. Do not replace the data directory.

Release maintainers must publish a tested stable GitHub Release before users receive an update notice. Merely pushing code or increasing the local version does not publish a release. Future automatic installation requires verified artifacts, platform-specific installation, job coordination, configuration backups, health checks and tested rollback.
