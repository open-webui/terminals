# Terminals

> [!WARNING]
> **🚧 Work in Progress** – This project is under active development and is not yet ready for production use. APIs, configuration, and features may change without notice.

> **Alpha** – APIs and configuration may change.

Multi-tenant terminal orchestrator for [Open Terminal](https://github.com/open-webui/open-terminal). Provisions isolated terminal instances per user with automatic lifecycle management.

## Getting Started

### Install & Run

```bash
pip install -e .
terminals serve
```

The server starts on `http://0.0.0.0:3000`. An API key is auto-generated and printed to the console.

### Docker

```bash
docker build -t terminals .
docker run -p 3000:3000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v $(pwd)/data:/app/data \
  terminals
```

### Development

```bash
uv sync
./dev.sh  # uvicorn with --reload
```

Frontend (admin dashboard):

```bash
cd terminals/frontend
npm install && npm run dev
```

## Usage

All Open Terminal endpoints are available under `/terminals/`. Include `X-User-Id` to identify the user.

```bash
export API_KEY="<api-key>"
export HEADERS=(-H "Authorization: Bearer $API_KEY" -H "X-User-Id: user-123")

curl -X POST http://localhost:3000/terminals/execute \
  "${HEADERS[@]}" -H "Content-Type: application/json" \
  -d '{"command": "echo hello"}'

curl http://localhost:3000/terminals/files/list "${HEADERS[@]}"

curl "http://localhost:3000/terminals/files/read?path=README.md" "${HEADERS[@]}"
```

Interactive terminal sessions connect via WebSocket at `/terminals/api/terminals/{session_id}`. Admins can manage tenants via `/api/v1/tenants/`.

## Configuration

Settings are loaded from environment variables prefixed with `TERMINALS_` (or a `.env` file).

| Variable | Default | Description |
|----------|---------|-------------|
| `TERMINALS_BACKEND` | `docker` | `docker`, `kubernetes`, `kubernetes-operator`, `local`, `static` |
| `TERMINALS_API_KEY` | *(auto-generated)* | Bearer token for API auth |
| `TERMINALS_OPEN_WEBUI_URL` | | Open WebUI URL for JWT auth |
| `TERMINALS_DATABASE_URL` | `sqlite+aiosqlite:///./data/terminals.db` | SQLAlchemy async URL |
| `TERMINALS_IMAGE` | `ghcr.io/open-webui/open-terminal:latest` | Docker/K8s container image |
| `TERMINALS_IDLE_TIMEOUT_SECONDS` | `1800` | Stop idle instances (0 = disabled) |
| `TERMINALS_PORT` | `3000` | Server port |

See [`config.py`](terminals/config.py) for the full list including Kubernetes, static backend, and encryption settings.

## Authentication

| Mode | Trigger | How it works |
|------|---------|-------------|
| **Open WebUI JWT** | Set `TERMINALS_OPEN_WEBUI_URL` | Validates tokens against Open WebUI |
| **API Key** | Set `TERMINALS_API_KEY` | Static bearer token |
| **Open** | Neither set | No auth (development only) |

## Backends

- **`docker`** – One container per user via Docker socket
- **`kubernetes`** – Pod + PVC + Service per user via K8s API
- **`kubernetes-operator`** – Delegates to a Kopf operator watching `Terminal` CRDs
- **`local`** – Spawns `open-terminal` as a subprocess (dev/testing)
- **`static`** – Proxies all users to a single pre-running instance

## Database

SQLite works out of the box. For PostgreSQL:

```bash
pip install asyncpg
export TERMINALS_DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/terminals
```

Migrations run automatically on startup. Manual management:

```bash
terminals db upgrade          # run pending migrations
terminals db current          # show revision
terminals db revision -m "msg"  # create migration
```

## License

[Open WebUI Enterprise License](LICENSE)
