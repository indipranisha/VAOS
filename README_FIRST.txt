START HERE
==========
VAOS — Video Analytics - Operating System

1. Extract the folder.
2. Install Python 3.12 if needed (python.org; enable Add Python to PATH).
3. Double-click start_windows.bat.
4. Open http://127.0.0.1:8000 on the PC (private dashboard).
5. Use username admin. The five-digit numeric password is printed in the terminal, saved in data\admin_password.txt, and shown at the top of the private dashboard after sign-in.
6. The first setup installs the NVIDIA CUDA runtime on GPU systems and downloads YOLO26m automatically when object detection first runs. The dashboard can enroll from a photo or its live camera.
7. The launcher creates the HTTPS camera-only tunnel and opens the dashboard. Keep the launcher window open; press Enter there to stop all services.
8. The dashboard shows camera health, model status, object activity, enrolled profiles, and sightings. Object detection uses NVIDIA GPU 0 when available, with CPU fallback.
9. Read README.md for full setup and privacy notes.

This is an experimental POC. Use face matching only with informed permission and appropriate authorisation.

