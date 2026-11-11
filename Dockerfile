FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VAOS_DATA_DIR=/var/lib/vaos \
    MODEL_DIR=/var/lib/vaos/models \
    YOLO_CONFIG_DIR=/var/lib/vaos/ultralytics

WORKDIR /app
ARG INSTALL_OBJECT_DETECTION=true
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu

COPY requirements.txt requirements-object-detection.txt ./
RUN python -m pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && if [ "$INSTALL_OBJECT_DETECTION" = "true" ]; then \
         pip install --no-cache-dir --index-url "$TORCH_INDEX_URL" torch torchvision \
         && pip install --no-cache-dir -r requirements-object-detection.txt \
         && pip install --no-cache-dir --no-deps "ultralytics==8.4.183"; \
       fi \
    && useradd --system --uid 10001 --create-home vaos \
    && mkdir -p /var/lib/vaos/models /var/lib/vaos/ultralytics \
    && chown -R vaos:vaos /var/lib/vaos

COPY --chown=vaos:vaos main.py camera_service.py check_gpu_runtime.py ./
COPY --chown=vaos:vaos templates ./templates
COPY --chown=vaos:vaos static ./static

USER 10001:10001
EXPOSE 8000 8001
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD-SHELL python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('VAOS_HEALTH_PORT','8000')+'/health',timeout=3)" || exit 1

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
