FROM python:3.14.7-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

WORKDIR /app
COPY tests/e2e/mock_provider/app.py /app/app.py
USER 65534
EXPOSE 8090
ENTRYPOINT ["python", "/app/app.py"]
