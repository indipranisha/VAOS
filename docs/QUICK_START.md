# VAOS quick start

VAOS runs as two local web services: an administrator site and a camera/enrollment site. The cross-platform `start_vaos.py` launcher starts both on Windows, macOS and Linux from one terminal. Windows users can also use `start_windows.bat`, which includes the temporary camera tunnel. See the [installation guide](INSTALLATION.md) and [platform support matrix](PLATFORM_SUPPORT.md).

## First administrator session

1. Start VAOS using the installation guide and open `http://127.0.0.1:8000` on the same computer.
2. Sign in with the one-time local administrator password printed by the Windows launcher or saved at `data/admin_password.txt` for a new data directory. Do not share or commit it.
3. Check service status and available hardware before using cameras or training.
4. Create a camera link only for a device and person that you are authorized to monitor. Treat its URL/QR code as a secret. For remote phone access, use the configured HTTPS camera address and understand that the Windows quick tunnel URL can change.
5. For face enrollment, obtain informed consent. Face templates and images are sensitive; define who can access them and how they will be deleted.
6. To train an object detector, first prepare a supported dataset and review sample labels. Choose the starting weights, data, image size and epoch count in ML Training. Check the per-epoch metrics and logs; training can fail when labels, paths or hardware memory are invalid.
7. Review API and MCP access under Settings only if needed. It is disabled by default. Start with a read token and the Diagnostics group; keep secrets out of screenshots, source files and chat.

## Where to go next

- [User guide](user-guide.md): dashboard, reports and routine workflows.
- [Administrator guide](ADMIN_GUIDE.md): access, storage, backups and operations.
- [Camera and streaming guide](CAMERA_STREAMING.md): camera links, frame processing and privacy.
- [Model and training guide](MODEL_TRAINING.md): datasets, preparation, training and metric interpretation.
- [API guide](API.md) and [MCP guide](MCP.md): integrations and permission boundaries.
- [Security and privacy](SECURITY_PRIVACY.md): sensitive data and safe deployment.

VAOS is not certified for safety-critical decisions. Face similarity and object detections require validation for the intended setting.
