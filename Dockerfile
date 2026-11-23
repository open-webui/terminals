FROM node:22-slim AS frontend

WORKDIR /frontend

COPY machines/frontend/package.json machines/frontend/package-lock.json ./
RUN npm ci

COPY machines/frontend/ ./
RUN npm run build


FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY . .
COPY --from=frontend /frontend/build /app/machines/frontend/build

# Run the package from /app so its default data directory is /app/data.
RUN pip install --no-cache-dir --editable . \
    && mkdir -p /app/data \
    && chgrp -R 0 /app/data \
    && chmod -R g=u /app/data

EXPOSE 3000

ENTRYPOINT ["terminals"]
CMD ["serve"]
