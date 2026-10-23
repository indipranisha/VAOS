# Vibe-coding guide for this POC

This project is intentionally small enough to learn by changing one feature at a time. Use ChatGPT, Codex, Cursor, or another coding assistant with the project folder open.

## First prompt to give your coding assistant

> Read `README.md`, `main.py`, `templates/index.html`, `templates/camera.html`, and `static/style.css` before changing anything. This is a beginner-owned local-first proof of concept. Explain the current architecture to me in plain language. Keep face processing on the host PC, use only explicitly enrolled participants who have given permission, never retain camera frames or unknown-face templates, and do not add external AI APIs by default. Do not rewrite the whole app. First list the smallest useful next improvements, with their files and how I can test each one. Make changes only after I select an improvement. Preserve the current startup workflow and update the README when behavior changes.

## Recommended iteration order

1. **Run the app without changing code.** Verify the dashboard is password protected and `/health` returns `{"status":"ok", ...}`.
2. **Make one camera work.** Use a phone with the HTTPS access method documented in `README.md`.
3. **Test enrolment and matching with volunteers.** Vary lighting, angle and distance. Keep a record of false matches and misses.
4. **Improve the operator experience.** Suggested feature: clearer camera online/offline status and last-frame timestamp.
5. **Improve reliability and privacy.** Suggested features: retention period for events, a consent/audit record, and a reliable way to revoke a participant's data.
6. **Only then consider multiple simultaneous feeds, GPU acceleration, user roles, or deployment.**

## Prompt template for one feature

> Implement only this feature: **[describe one improvement]**. Before editing, explain which files will change and what could break. Keep the app local-first and preserve participant consent, no image retention, and unknown-face rejection. Add graceful error handling, update the relevant README section, and run available checks. At the end, tell me which files changed, the commands to run, and exactly how I can test the feature without needing to understand all the code.

## If something breaks

Ask the coding assistant to diagnose before rewriting:

> The app failed with this error: **[paste full terminal error]**. Diagnose the likely root cause. Inspect the existing code and dependency versions. Propose the smallest fix first; do not remove security, consent, or data-retention controls to make the error disappear.

## A note on hosted AI APIs

A hosted model is not necessary for this use case. Face matching is a computer-vision task and can run locally. A text-generation API may help explain errors or generate code, but it should not receive live frames or face templates. Keep any future hosted service optional and off by default.
