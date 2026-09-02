FROM python:3.12.11-slim@sha256:47ae396f09c1303b8653019811a8498470603d7ffefc29cb07c88f1f8cb3d19f

WORKDIR /app
COPY tests/e2e/mock_provider/app.py /app/app.py
USER 65534
EXPOSE 8090
ENTRYPOINT ["python", "/app/app.py"]
