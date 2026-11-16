VAOS 1.1.0 — Video Analytics Operating System

Windows quick start:
1. Use Windows 10/11 and Python 3.12.
2. Double-click start_windows.bat and leave the terminal open.
3. Open http://127.0.0.1:8000. Sign in as admin using the generated password printed by the launcher. The local password is also stored in data\admin_password.txt; protect that file.
4. The launcher starts the camera service on port 8001 and may start a temporary HTTPS tunnel for camera-only links. It is not an admin tunnel. Treat camera URLs as secrets.
5. Press Enter in the launcher terminal to stop its services and tunnel.

Start with README.md, then follow docs/INSTALLATION.md for installation and updates, docs/COMPLETE_USER_MANUAL.md for application workflows and uninstall, docs/CONFIGURATION_REFERENCE.md for environment variables, and docs/PROJECT_STATUS.md for tested limitations and release status.

Face photos and templates are sensitive biometric data. Obtain informed consent and protect data and backups. See docs/SECURITY_PRIVACY.md before enrollment or remote access.
