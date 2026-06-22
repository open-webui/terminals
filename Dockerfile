FROM node:22-slim AS frontend

WORKDIR /frontend

COPY terminals/frontend/package.json terminals/frontend/package-lock.json ./
RUN npm ci

COPY terminals/frontend/ ./
RUN npm run build


FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY . .
COPY --from=frontend /frontend/build /app/terminals/frontend/build

RUN pip install --no-cache-dir .

EXPOSE 3000

ENTRYPOINT ["terminals"]
CMD ["serve"]
