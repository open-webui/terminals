# Terminals

Run [Open Terminal](https://github.com/open-webui/open-terminal) environments from shared templates. Terminals starts containers on demand, routes requests by template and scope, and manages persistent files and idle cleanup on Docker or Kubernetes.

```text
Open WebUI → Terminals → Open Terminal containers
```

> [!IMPORTANT]
> Commercial or organizational production use requires an [Open WebUI Enterprise License](LICENSE) with Terminals access.

[Quick start](#quick-start) · [Admin UI](#admin-ui) · [Templates](#templates) · [Storage](#storage-and-persistence) · [Kubernetes](#kubernetes-deployment) · [Configuration](#configuration) · [Troubleshooting](#troubleshooting)

## Quick start

You need Docker running and an existing Open WebUI deployment. These commands use a macOS/Linux shell or WSL, with `openssl` available to generate a key. No repository clone or separate database is needed.

### 1. Start Terminals

Run this in the directory where you want to keep your terminal data:

```bash
mkdir -p data
export API_KEY="$(openssl rand -hex 32)"

docker network create terminals

docker run -d \
  --name terminals \
  --restart unless-stopped \
  --network terminals \
  -p 127.0.0.1:3000:3000 \
  -e TERMINALS_API_KEY="$API_KEY" \
  -e TERMINALS_DOCKER_NETWORK=terminals \
  -e TERMINALS_DOCKER_HOST_DATA_DIR="$(pwd)/data/terminals" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$(pwd)/data:/app/data" \
  ghcr.io/open-webui/terminals:latest

printf 'API key: %s\n' "$API_KEY"
```

Save the printed key. Use the same key when recreating the service. If the `terminals` network already exists, skip the network creation command.

The service and the terminal containers use the same Docker network, so no host gateway configuration is needed. The `data` directory holds the database and users' terminal files.

### 2. Open the admin UI

Open **[http://localhost:3000](http://localhost:3000)**, paste the key into **Admin key**, and click **Refresh**. An empty Environments list is expected until someone uses a template.

Check that the service is running:

```bash
curl -fsS http://localhost:3000/health
```

Expected response: `{"status":true}`. If it is not ready, check `docker logs terminals`.

### 3. Connect Open WebUI

If Open WebUI runs in Docker, attach its container to the same network. Replace `open-webui` with your container's name; skip this if it is already attached:

```bash
docker network connect terminals open-webui
```

In Open WebUI, open **Settings → Admin → Integrations → Open Terminal** and add a connection:

| Field | Value |
|-------|-------|
| URL | `http://terminals:3000` when Open WebUI is on the Docker network above |
| Auth | `Bearer` |
| API Key | The key printed in step 1 |

If Open WebUI runs directly on the same host, use `http://localhost:3000` instead. The URL must be reachable from the Open WebUI server. The quick start binds the admin port to localhost; for remote access, expose it through your usual authenticated HTTPS deployment.

Verify the connection and save it. In the terminal menu, choose **New terminal**, enter a name, choose a scope, and save. Select that template to use it. Terminals creates its environment on first use; the first request can take longer while its image is downloaded.

See the [Open WebUI connection guide](https://docs.openwebui.com/features/open-terminal/setup/connecting/) for the full UI walkthrough.

<details>
<summary>Try a terminal directly with curl</summary>

Create a user-scoped template, then execute as the example user `demo-user`:

```bash
curl -fsS -X POST http://localhost:3000/api/v1/terminals/templates \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"id":"demo","data":{"name":"My workspace","scope":"user"}}'
```

```bash
curl -fsS http://localhost:3000/execute \
  -H "Authorization: Bearer $API_KEY" \
  -H "X-User-Id: demo-user" \
  -H "X-Terminal-Template-Id: demo" \
  -H "Content-Type: application/json" \
  -d '{"command":"echo hello"}'
```

The examples below use the same `$API_KEY` shell variable. In a new shell, set it to your saved key.

</details>

## Admin UI

The built-in UI at `/` has two sections:

- **Templates:** create or edit instance configuration and scheduled resets, or delete a template and its environments.
- **Environments:** inspect tracked environments, including stopped ones; start, stop, restart, reset, or delete the selected environment.

Use the Terminals admin API key. Set `TERMINALS_ENABLE_UI=false` for an API-only deployment.

## Templates in Open WebUI

Connect a Terminals server using its server key. In the terminal picker, choose **New terminal**, enter a name and instance configuration, and choose one scope:

| Scope | Environment |
|-------|-------------|
| None (`null`) | One environment for everyone authorized to use the template |
| User (`"user"`) | One environment per user |
| Chat (`"chat"`) | One environment per saved chat, shared by its authorized collaborators |
| Model (`"model"`) | One environment per selected WebUI model or preset |

Missing required context is an error. Creating two independent terminals creates two templates. Renaming a template does not change its environment identity or files.

Open WebUI stores access grants per template. **Use** permits commands and file changes; **Manage** also permits configuration, sharing, and environment management. New templates are private to their creator. Parent connection access remains required. Snapshot-only chat sharing grants no execution access.

Templates are managed at `/api/v1/terminals/templates`. Execution uses the existing proxy with `X-Terminal-Template-Id` and verified user, chat, or model headers from WebUI. Managed requests require `TERMINALS_API_KEY`; Terminals does not call WebUI to authorize them.

Persistent homes live under `.terminals/<terminal-id>` on Docker and shared Kubernetes storage; dedicated Kubernetes storage uses a separate PVC. The terminal ID is derived from the template and scope context. Restart preserves files. Reset clears only the selected environment's files. Deleting a template removes its environments and files.

For existing deployments, see the [0.4.0 upgrade requirements](CHANGELOG.md#moving-to-040-manually). Existing populated deployments use the documented manual migration to a new database and separate storage; `terminals db upgrade` does not convert them.

## Templates

A template defines the terminal's scope, image, environment variables, and resource limits. Create one in the admin UI or through the API; fresh installations do not create a default template automatically.

```bash
curl -fsS -X POST http://localhost:3000/api/v1/terminals/templates \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "data-science",
    "data": {
      "name": "Data science",
      "scope": "user",
      "image": "ghcr.io/open-webui/open-terminal:latest",
      "cpu_limit": "2",
      "memory_limit": "4Gi",
      "idle_timeout_minutes": 30
    }
  }'
```

Select the template in the execution headers:

```bash
curl -fsS http://localhost:3000/execute \
  -H "Authorization: Bearer $API_KEY" \
  -H "X-User-Id: demo-user" \
  -H "X-Terminal-Template-Id: data-science" \
  -H "Content-Type: application/json" \
  -d '{"command":"echo hello"}'
```

| Field | Purpose |
|-------|---------|
| `name` | Display name; changing it preserves instance identity |
| `scope` | `null`, `"user"`, `"chat"`, or `"model"` |
| `image` | Open Terminal image to run |
| `env` | Environment variables passed to the terminal |
| `network_profile` | Optional name from `TERMINALS_NETWORK_PROFILES`; omitted uses existing networking defaults |
| `cpu_limit`, `memory_limit` | Resource limits, such as `"2"` and `"4Gi"` |
| `storage` | Storage size, such as `"5Gi"`; behavior depends on the backend below |
| `storage_mode` | Direct Kubernetes: `per-user`, `shared`, or `shared-rwo` |
| `idle_timeout_minutes` | Stop terminals after this many idle minutes; `0` disables idle cleanup |
| `max_lifetime_minutes` | Stop terminals this many minutes after they start, even while in use; `0` disables the limit |
| `restricted` | Restricted Kubernetes/OpenShift defaults |
| `pod_security_context`, `container_security_context` | Kubernetes security context overrides |
| `podTemplate` | Kubernetes pod overrides, including volumes, scheduling, and health probes |

Service limits such as `TERMINALS_MAX_CPU`, `TERMINALS_MAX_MEMORY`, and `TERMINALS_ALLOWED_IMAGES` constrain template settings.

### Network profiles

Define named profiles in the orchestrator's environment, then select one with
`"network_profile": "restricted"` in a template or the admin template editor:

```bash
TERMINALS_NETWORK_PROFILES='{"restricted":{"docker_network":"terminals-restricted","kubernetes_labels":{"network-tier":"restricted"}}}'
```

Only the mapping for the active backend is required. Unknown names, missing
backend mappings, and invalid mapping types are rejected. Omit `network_profile`
to preserve existing networking behavior.

- **Docker:** `docker_network` selects existing named networks, with the same
  comma-separated sharding as `TERMINALS_DOCKER_NETWORK`. Attach the orchestrator
  to every selected network so it can reach terminals by name. Built-in modes
  such as `host` and `none` are not supported by profiles.
- **Kubernetes and Kubernetes operator:** `kubernetes_labels` adds pod labels for
  administrator-installed NetworkPolicies. Profile labels override conflicting
  pod-template labels; `openwebui.com/` and `app.kubernetes.io/` labels are reserved.
  The operator uses its existing `podTemplate` support, so no CRD change is needed.

Profiles select configuration; they do not create networks, firewall rules, or
NetworkPolicies. Configure Docker firewall rules or Kubernetes NetworkPolicies
and a CNI that enforces them to provide the intended access restrictions. A name
such as `restricted` alone does not enforce isolation. Allow the orchestrator to
reach terminal port 8000 and permit DNS if needed. Use the restart operation below to apply
profile changes to existing terminals.

### Apply changes to existing terminals

Edit configuration with `PATCH /api/v1/terminals/templates/{id}`. Changes take effect when an environment next starts. Restart stops the selected environment, preserves its files, and starts it again with the current template configuration:

```bash
curl -fsS -X POST http://localhost:3000/api/v1/terminals/templates/data-science/restart \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"user_id":"demo-user"}'
```

For chat or model scope, also supply `chat_id` or `model_id`. Restart does not create an unused environment. Use `/stop` to stop it without starting it again, and `/start` to start it, creating it if needed. Scope and storage-mode changes are rejected while affected environments exist.

### Schedule file resets

Scheduled resets delete persisted terminal files and refresh matching terminals, including active ones. Configure them in the template editor or with:

```bash
curl -fsS -X PUT http://localhost:3000/api/v1/terminals/templates/data-science/lifecycle \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"reset":{"schedule":"@weekly","timezone":"UTC"}}'
```

Schedules support one-time ISO datetimes, `@weekly`, `@monthly`, and five-field cron expressions. Docker clears the matching home directory. Kubernetes uses a reset pod to clear the matching PVC or environment subdirectory.

## Storage and persistence

Stopping or reaping a terminal preserves its persisted files. Idle cleanup is disabled by default; enable it with `TERMINALS_IDLE_TIMEOUT_MINUTES` or a template. A maximum lifetime, set with `TERMINALS_MAX_LIFETIME_MINUTES` or a template, stops terminals after a fixed time even while they are in use; the next request starts a fresh one with the same files. File resets and environment deletion are separate, destructive actions.

| Backend | Persisted terminal files |
|---------|-------------------------|
| Docker | Bind-mounted under `TERMINALS_DOCKER_DATA_DIR`, regardless of the template's `storage` field |
| Kubernetes | PVCs; defaults to `1Gi` per terminal, with optional shared storage modes |
| Kubernetes operator | Dedicated PVCs; `storage` overrides the server default size |

### Docker paths

The quick start mounts the host's `./data` directory at `/app/data`:

| Host path | Container path | Contents |
|-----------|----------------|----------|
| `./data/terminals.db` | `/app/data/terminals.db` | SQLite database: templates and lifecycle state |
| `./data/terminals/` | `/app/data/terminals/` | Terminal homes under `.terminals/<terminal-id>` |

`TERMINALS_DOCKER_HOST_DATA_DIR` tells the Docker daemon where those same terminal files live on its host. It is optional when the orchestrator and daemon use the same path. Both paths must refer to the same storage so provisioning, resets, and deletion operate on the same files. The quick start assumes a local Docker daemon.

Back up the database and terminal files before upgrades. Changing paths does not migrate existing data or change mounts on existing containers. Move data from its previous effective location before changing the configuration.

### Storage limits

On Kubernetes, `storage` sets the requested PVC size. On Docker it caps only the container's writable layer, and only when the storage driver supports `StorageOpt` (for example, overlay2 on XFS with `pquota`). Unsupported drivers log a warning and run without that limit. The bind-mounted `/home/user` directory is not covered by Docker's limit.

Terminal files need a filesystem. Open WebUI's uploaded-file storage and Azure Blob cleanup do not manage them. For Azure, use Azure Files, managed disks, or Kubernetes PVCs for terminal homes.

### Delete an environment's files

Deleting an Open WebUI account does not automatically delete terminal storage. Inspect a template's tracked instances with `GET /api/v1/terminals/templates/{id}/instances` (or all instances with `GET /api/v1/terminals`), then delete the specific template/context:

```bash
curl -fsS -X DELETE http://localhost:3000/api/v1/terminals/templates/data-science/instance \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"user_id":"deleted-user-id"}'
```

Use the context required by the template's scope. Chat- and model-scoped environments are shared; they do not belong to the first user who opened them. Deletion stops the workload before removing its storage and lifecycle record. Failed cleanup retains records so it can be retried. Selecting the same template/context later creates an empty environment.

Shared PVC deletion clears only the selected environment's subdirectory. Dedicated PVC deletion follows the storage class's reclaim policy; retained volumes and backups need separate removal.

## Kubernetes deployment

| Backend | Use when |
|---------|----------|
| `docker` | Running on a single Docker host; the quick-start default |
| `kubernetes-operator` | An operator should manage `Terminal` custom resources |
| `kubernetes` | Terminals should manage Pods, PVCs, and Services directly, without a CRD |

From a checkout of this repository, install the operator:

```bash
kubectl apply -f manifests/terminal-crd.yaml
kubectl apply -f manifests/operator-deployment.yaml
```

These commands install the operator, not the Terminals API service. Deploy `ghcr.io/open-webui/terminals:latest` separately with:

```yaml
env:
  - name: TERMINALS_BACKEND
    value: kubernetes-operator
  - name: TERMINALS_KUBERNETES_NAMESPACE
    value: terminals
  - name: TERMINALS_API_KEY
    valueFrom:
      secretKeyRef:
        name: terminals
        key: api-key
```

Create the referenced Secret, mount persistent storage at `/app/data` for SQLite, and give the service account the backend's required Kubernetes permissions. The operator RBAC is in [operator-deployment.yaml](manifests/operator-deployment.yaml). Expose the Terminals service on port `3000` to Open WebUI. Add `"storage":"5Gi"` to operator templates to request that PVC size.

For direct Kubernetes provisioning, use `TERMINALS_BACKEND=kubernetes`; no operator or CRD is required. For restricted OpenShift clusters, follow [OpenShift deployment](docs/openshift.md).

## Authentication

The quick start uses a static `TERMINALS_API_KEY` for both the admin UI and Open WebUI's Bearer connection. Keep it on trusted servers: API-key requests trust the supplied `X-User-Id` to select the user.

Managed execution uses the server key. WebUI checks access and forwards verified context; user JWTs do not authorize managed template requests.

Tool discovery at `/openapi.json` uses the server API key and the same verified template, user, and scope context as execution. It uses that instance; there is no separate discovery instance.

`terminals serve` generates and prints a key if none is configured. That generated key changes when the service process restarts. Configure a persistent API key for deployments; managed execution requires it even when running the ASGI app directly.

## Configuration

All settings are configured through environment variables prefixed with `TERMINALS_`, or via a `.env` file.

### Common

| Variable | Default | Description |
|----------|---------|-------------|
| `TERMINALS_BACKEND` | `docker` | `docker`, `kubernetes`, or `kubernetes-operator` |
| `TERMINALS_API_KEY` | *(auto-generated)* | Bearer token for API auth |
| `TERMINALS_HOST` | `0.0.0.0` | Orchestrator API bind host |
| `TERMINALS_PORT` | `3000` | Orchestrator API bind port |
| `TERMINALS_WORKERS` | `1` | Uvicorn worker process count. Docker workers adopt existing containers by deterministic name instead of replacing them. |
| `TERMINALS_ENABLE_UI` | `true` | Serve the built-in minimal admin UI at `/`. Set to `false` for API-only deployments. |
| `TERMINALS_IMAGE` | `ghcr.io/open-webui/open-terminal:latest` | Default Open Terminal container image |
| `TERMINALS_IMAGE_PULL_POLICY` | | `Always`, `IfNotPresent`, or `Never`, for every backend. Unset pulls images tagged `:latest` or untagged on every start and other tags only when missing, matching Kubernetes. |
| `TERMINALS_IMAGE_PULL_INTERVAL_SECONDS` | `3600` | Docker only: how long a pulled image is used before `Always` checks the registry again. If the registry can't be reached, the local copy is used. |
| `TERMINALS_NETWORK_PROFILES` | `{}` | JSON object mapping profile names to `docker_network` and/or `kubernetes_labels` configuration |
| `TERMINALS_MAX_CPU` | | Hard cap on CPU per terminal container/pod |
| `TERMINALS_MAX_MEMORY` | | Hard cap on memory per terminal container/pod |
| `TERMINALS_MAX_STORAGE` | | Storage cap. Kubernetes enforces this with PVC sizes; Docker support depends on the storage driver and does not quota the bind-mounted `/home/user`. |
| `TERMINALS_ALLOWED_IMAGES` | | Comma-separated list of allowed image patterns |
| `TERMINALS_IDLE_TIMEOUT_MINUTES` | `0` | Minutes of inactivity before terminals are torn down. `0` disables idle cleanup. |
| `TERMINALS_MAX_LIFETIME_MINUTES` | `0` | Minutes after start before terminals are torn down, even while in use. `0` disables the limit. |
| `TERMINALS_IDLE_CLEANUP_TIMEOUT_SECONDS` | `120` | Timeout for each teardown/reset call during idle cleanup |
| `TERMINALS_DATABASE_URL` | `sqlite+aiosqlite:////app/data/terminals.db` in the image | SQLAlchemy database URL. Source installs use `data/terminals.db` next to the package directory. PostgreSQL is optional. |
| `TERMINALS_LOG_LEVEL` | `INFO` | Minimum orchestrator log level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. On Docker, `WARNING` or higher disables child container Docker logs because Open Terminal does not expose a log-level env var. |
| `TERMINALS_STATUS_CACHE_TTL` | `30` | Seconds a confirmed-running container status is trusted before re-inspecting it via the backend. `0` re-checks on every request. |
| `TERMINALS_WS_COMPRESSION` | `false` | Enable permessage-deflate on proxied WebSocket terminal traffic. Leave off unless clients connect over slow links. |
| `TERMINALS_ACCESS_LOG` | `false` | Log every HTTP request. Off by default because per-request logging is expensive at high request rates. |
| `TERMINALS_REPLAY_BODY_LIMIT` | | Maximum proxied request body bytes buffered for retry. Unset, `none`, `null`, or `unlimited` means no size cap. |
| `TERMINALS_PROXY_CONNECT_TIMEOUT_SECONDS` | `10` | Timeout for opening upstream proxy connections to terminal containers |
| `TERMINALS_PROXY_READ_TIMEOUT_SECONDS` | `360` | Timeout while waiting for upstream proxy responses. Keep above Open Terminal's maximum `/execute?wait` value. |
| `TERMINALS_PROXY_WRITE_TIMEOUT_SECONDS` | `300` | Timeout while sending proxied request bodies upstream |
| `TERMINALS_PROXY_POOL_TIMEOUT_SECONDS` | `300` | Timeout while waiting for an upstream proxy connection from the pool |
| `TERMINALS_PROVISION_TIMEOUT_SECONDS` | `120` | Timeout waiting for terminal readiness on the `kubernetes-operator` backend |

### Docker

| Variable | Default | Description |
|----------|---------|-------------|
| `TERMINALS_DOCKER_HOST` | `127.0.0.1` | Host/IP that the orchestrator uses to reach published terminal container ports |
| `TERMINALS_DOCKER_NETWORK` | | Docker network for terminal containers. Use a comma-separated list to shard terminals across multiple Docker bridge networks. |
| `TERMINALS_DOCKER_MOUNTS` | | JSON array of extra bind mounts with `source`, `target`, and optional `readOnly` |
| `TERMINALS_DOCKER_DATA_DIR` | `data/terminals` (`/app/data/terminals` in the image) | Workspace directory accessible to the orchestrator, including file resets |
| `TERMINALS_DOCKER_HOST_DATA_DIR` | | Optional absolute Docker-host path to the same workspace directory. Unset uses `TERMINALS_DOCKER_DATA_DIR`. |

### Kubernetes

| Variable | Default | Description |
|----------|---------|-------------|
| `TERMINALS_KUBERNETES_NAMESPACE` | `terminals` | Namespace for terminal pods, services, PVCs, and CRDs |
| `TERMINALS_KUBERNETES_STORAGE_CLASS` | | Storage class for terminal PVCs. Empty uses the cluster default. |
| `TERMINALS_KUBERNETES_STORAGE_SIZE` | `1Gi` | Default PVC size for the direct `kubernetes` backend; both Kubernetes backends use this default when template `storage` is omitted |
| `TERMINALS_KUBERNETES_STORAGE_MODE` | `per-user` | Direct Kubernetes storage mode: `per-user`, `shared`, or `shared-rwo`; the operator creates dedicated PVCs |
| `TERMINALS_KUBERNETES_SERVICE_TYPE` | `ClusterIP` | Service type for terminal services |
| `TERMINALS_KUBERNETES_KUBECONFIG` | | Kubeconfig path. Empty uses in-cluster config. |
| `TERMINALS_KUBERNETES_LABELS` | | Extra labels for terminal resources, as `k=v,k2=v2` |
| `TERMINALS_KUBERNETES_RESTRICTED` | `false` | Enable restricted Kubernetes/OpenShift pod defaults globally |
| `TERMINALS_KUBERNETES_POD_SECURITY_CONTEXT` | | JSON pod security context merged into terminal pods |
| `TERMINALS_KUBERNETES_CONTAINER_SECURITY_CONTEXT` | | JSON container security context merged into terminal containers |
| `TERMINALS_KUBERNETES_NODE_SELECTOR` | | Node selector for terminal and reset pods, as JSON or `k=v,k2=v2` |
| `TERMINALS_KUBERNETES_TOLERATIONS` | | JSON array of tolerations for terminal and reset pods |

### Kubernetes operator

| Variable | Default | Description |
|----------|---------|-------------|
| `TERMINALS_KUBERNETES_CRD_GROUP` | `openwebui.com` | Terminal CRD API group |
| `TERMINALS_KUBERNETES_CRD_VERSION` | `v1alpha1` | Terminal CRD API version |

These tables are the public environment-variable surface. Compatibility-only
settings may still be accepted by the code but are intentionally omitted.

By default, known-size proxied request bodies are buffered so retry behavior is preserved. Set `TERMINALS_REPLAY_BODY_LIMIT` to stream request bodies above that byte limit instead of buffering them in orchestrator memory. Chunked uploads are always streamed one-shot and are not retried.

## Advanced configuration

### Extra mounts


Docker deployments can use `TERMINALS_DOCKER_MOUNTS` to mount shared data into
every spawned terminal container:

```bash
TERMINALS_DOCKER_MOUNTS='[
  {"source": "/srv/datasets", "target": "/mnt/datasets", "readOnly": true},
  {"source": "/srv/shared-work", "target": "/workspace/shared", "readOnly": false}
]'
```

`source` is an absolute path on the Docker daemon host. `target` is the absolute
container path. `readOnly` defaults to `true`. Do not mount over `/home/user`;
Open Terminal expects that directory to stay writable.

Kubernetes deployments do not use `TERMINALS_DOCKER_MOUNTS`. Use the existing
template `podTemplate` field and native Kubernetes volumes instead:

```json
{
  "podTemplate": {
    "spec": {
      "containers": [
        {
          "name": "open-terminal",
          "volumeMounts": [
            {"name": "datasets", "mountPath": "/mnt/datasets", "readOnly": true}
          ]
        }
      ],
      "volumes": [
        {"name": "datasets", "persistentVolumeClaim": {"claimName": "datasets"}}
      ]
    }
  }
}
```

Read-only mounts prevent writes through that mount, but they are not a security
boundary if terminal containers are privileged or can access the Docker socket.

### Kubernetes health probes


Both Kubernetes backends accept probe overrides in the template's `podTemplate`.
Each supplied probe replaces the default, so include its check (such as
`httpGet`) as well as timing settings. For slower hardware:

```json
{
  "podTemplate": {
    "spec": {
      "containers": [{
        "name": "open-terminal",
        "readinessProbe": {
          "httpGet": {"path": "/health", "port": 8000},
          "initialDelaySeconds": 15,
          "periodSeconds": 10,
          "timeoutSeconds": 5,
          "failureThreshold": 6
        },
        "livenessProbe": {
          "httpGet": {"path": "/health", "port": 8000},
          "initialDelaySeconds": 60,
          "periodSeconds": 15,
          "timeoutSeconds": 5,
          "failureThreshold": 6
        }
      }]
    }
  }
```

Omitted probes keep their existing defaults. Restart existing terminals to apply
the updated template on their next access.

### Docker network scaling

For larger Docker deployments, create multiple bridge networks and set `TERMINALS_DOCKER_NETWORK` to a comma-separated list. Terminals assigns each template/context to a stable network. Attach the orchestrator container to every listed network so it can reach terminals by name.

## Troubleshooting

| Problem | What to check |
|---------|---------------|
| Admin UI shows `401` | Paste the Terminals API key, then click Refresh. Use the saved key, not an Open WebUI user token. |
| Open WebUI cannot connect | Use `http://terminals:3000` on the shared Docker network. Inside a container, `localhost` refers to that container. |
| First terminal is slow or fails | Check `docker logs terminals`; the configured terminal image must be available or pullable. |
| Files disappear after recreating the service | Check the `/app/data` bind mount and both Docker data paths. Keep the same host directory across recreations. |
| Kubernetes terminal files disappear after idle cleanup | Check that the environment PVC still exists and `/home/user` is mounted from it. |
| Environment deletion returns `409` | The workload may still be stopping. Check the returned error and retry; files are not deleted while the workload remains. |
| UI is unavailable in a source install | Build the frontend and ensure `TERMINALS_ENABLE_UI` is not `false`. |

## Development

Use Python 3.11+ and Node.js 22.12+:

```bash
git clone https://github.com/open-webui/terminals.git
cd terminals
python -m venv .venv
source .venv/bin/activate
pip install -e .
npm --prefix terminals/frontend ci
npm --prefix terminals/frontend run build
terminals serve
```

Docker must be available when using the default backend. The command prints the generated API key; open `http://localhost:3000` to use the admin UI. During frontend development, run `npm --prefix terminals/frontend run dev`; its API proxy targets the backend on port `3000`.

## License

Free for personal, non-commercial use. Commercial or organizational production use requires an Open WebUI Enterprise License. All rights reserved. See [LICENSE](LICENSE).
