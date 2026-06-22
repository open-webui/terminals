FROM node:22-slim AS frontend

WORKDIR /frontend

COPY terminals/frontend/package.json terminals/frontend/package-lock.json ./
RUN npm ci

COPY terminals/frontend/ ./
RUN npm run build


FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY . .
COPY --from=frontend /frontend/build /app/terminals/frontend/build

RUN pip install --no-cache-dir . \
    && mkdir -p /app/data \
    && chgrp -R 0 /app/data \
    && chmod -R g=u /app/data

EXPOSE 3000

ENTRYPOINT ["terminals"]
CMD ["serve"]
