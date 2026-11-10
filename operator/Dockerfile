FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    kopf>=1.37.0 \
    kubernetes>=31.0.0

COPY operator/handler.py /app/handler.py
COPY terminals/utils/kubernetes_labels.py /app/terminals/utils/kubernetes_labels.py

ENV PYTHONPATH=/app

USER 65534:65534

ENTRYPOINT ["kopf", "run", "/app/handler.py", "--verbose"]
