# Machines API (Terminals)

Run [Machine (Open Terminal)](https://github.com/open-webui/open-terminal) environments from shared templates. The service starts containers on demand, routes requests by template and scope, and manages persistent files and idle cleanup on Docker or Kubernetes.

**Requires Open WebUI 0.13.4 or later.**

```text
Open WebUI → Machines → Machine (Open Terminal) containers
```

> [!IMPORTANT]
> Commercial or organizational production use requires an [Open WebUI Enterprise License](LICENSE) with Terminals access.

[Quick start](#quick-start) · [Admin UI](#admin-ui) · [Templates](#templates) · [Storage](#storage-and-persistence) · [Kubernetes](#kubernetes-deployment) · [Configuration](#configuration) · [Troubleshooting](#troubleshooting)

## Quick start

You need Docker running and an existing Open WebUI deployment. These commands use a macOS/Linux shell or WSL, with `openssl` available to generate a key. No repository clone or separate database is needed.

### 1. Start Machines

Run this in the directory where you want to keep your machine data:

```bash
mkdir -p data
export API_KEY="$(openssl rand -hex 32)"

docker network create terminals

docker run -d \
  --name terminals \
  --restart unless-stopped \
  --network terminals \
  -p 127.0.0.1:3000:3000 \
  -e MACHINES_API_KEY="$API_KEY" \
  -e MACHINES_DOCKER_NETWORK=terminals \
  -e MACHINES_DOCKER_HOST_DATA_DIR="$(pwd)/data/terminals" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$(pwd)/data:/app/data" \
  ghcr.io/open-webui/terminals

printf 'API key: %s\n' "$API_KEY"
```

Save the printed key. Use the same key when recreating the service. If the `terminals` network already exists, skip the network creation command.

The service and the Machine (Open Terminal) containers use the same Docker network, so no host gateway configuration is needed. The `data` directory holds the database and users' workspace files.

### 2. Open the admin UI

Open **[http://localhost:3000](http://localhost:3000)**, paste the key into **Server key**, and click **Connect**. An empty Environments list is expected until someone uses a template.

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

In Open WebUI, open **Settings → Admin → Integrations** and add a Machine (Open Terminal) connection. Look under **Machines** in current versions or the runtime’s name in older versions.

| Field | Value |
|-------|-------|
| URL | `http://terminals:3000` when Open WebUI is on the Docker network above |
| Auth | `Bearer` |
| API Key | The key printed in step 1 |

If Open WebUI runs directly on the same host, use `http://localhost:3000` instead. The URL must be reachable from the Open WebUI server. The quick start binds the admin port to localhost; for remote access, expose it through your usual authenticated HTTPS deployment.

Verify the connection and save it. In the machine menu, choose **New machine**, enter a name, choose a scope, and save. Select that template to use it. The service creates the environment on first use; the first request can take longer while its image is downloaded.

See the [Open WebUI connection guide](https://docs.openwebui.com/features/open-terminal/setup/connecting/) for the full UI walkthrough.

<details>
<summary>Try a machine directly with curl</summary>

Create a user-scoped template, then execute as the example user `demo-user`:

```bash
curl -fsS -X POST http://localhost:3000/api/v1/machines/templates \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"id":"demo","data":{"name":"My workspace","scope":"user"}}'
```

```bash
curl -fsS http://localhost:3000/execute \
  -H "Authorization: Bearer $API_KEY" \
  -H "X-User-Id: demo-user" \
  -H "X-Machine-Template-Id: demo" \
  -H "Content-Type: application/json" \
  -d '{"command":"echo hello"}'
```

The examples below use the same `$API_KEY` shell variable. In a new shell, set it to your saved key.

</details>

## Admin UI

The built-in UI at `/` has two sections:

- **Templates:** create or edit instance configuration and scheduled resets, or delete a template and its environments.
- **Environments:** inspect tracked environments, including stopped ones; start, stop, restart, reset, or delete the selected environment.

Use the Machines admin API key. Set `MACHINES_ENABLE_UI=false` for an API-only deployment.

## Templates in Open WebUI

Connect a Machines server using its server key. In the machine picker, choose **New machine**, enter a name and instance configuration, and choose one scope:

| Scope | Environment |
|-------|-------------|
| None (`null`) | One environment for everyone authorized to use the template |
| User (`"user"`) | One environment per user |
| Chat (`"chat"`) | One environment per saved chat, shared by its authorized collaborators |
| Model (`"model"`) | One environment per selected WebUI model or preset |

Missing required context is an error. Creating two independent machines creates two templates. Renaming a template does not change its environment identity or files.

Open WebUI stores access grants per template. **Use** permits commands and file changes; **Manage** also permits configuration, sharing, and environment management. New templates are private to their creator. Parent connection access remains required. Snapshot-only chat sharing grants no execution access.

Templates are managed at `/api/v1/machines/templates`. The legacy prefix `/api/v1/terminals` supports the same admin operations. For HTTP, WebSocket, and OpenAPI requests, send `X-User-Id` and `X-Machine-Template-Id`. Chat and model scopes also require `X-Machine-Chat-Id` or `X-Machine-Model-Id`, respectively. Each routing header also accepts its original `X-Terminal-*` spelling. If both spellings are sent, their values must match. Managed requests require `MACHINES_API_KEY`; the service does not call WebUI to authorize them.

Persistent homes live under `.terminals/<instance-id>` on Docker and shared Kubernetes storage; dedicated Kubernetes storage uses a separate PVC. The instance ID is derived from the template and scope context. Restart preserves files. Reset clears only the selected environment's files. Deleting a template removes its environments and files.

For existing deployments, see the [0.4.0 upgrade requirements](CHANGELOG.md#moving-to-040-manually). Existing populated deployments use the documented manual migration to a new database and separate storage; `terminals db upgrade` does not convert them.

## Templates

A template defines the machine's scope, image, environment variables, and resource limits. Create one in the admin UI or through the API; fresh installations do not create a default template automatically.

```bash
curl -fsS -X POST http://localhost:3000/api/v1/machines/templates \
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
  -H "X-Machine-Template-Id: data-science" \
  -H "Content-Type: application/json" \
  -d '{"command":"echo hello"}'
```

| Field | Purpose |
|-------|---------|
| `name` | Display name; changing it preserves instance identity |
| `scope` | `null`, `"user"`, `"chat"`, or `"model"` |
| `image` | Machine (Open Terminal) image to run |
| `env` | Environment variables passed to the machine |
| `network_profile` | Optional name from `MACHINES_NETWORK_PROFILES`; omitted uses existing networking defaults |
| `cpu_limit`, `memory_limit` | Resource limits, such as `"2"` and `"4Gi"` |
| `storage` | Storage size, such as `"5Gi"`; behavior depends on the backend below |
| `storage_mode` | Direct Kubernetes: `per-user`, `shared`, or `shared-rwo` |
| `idle_timeout_minutes` | Stop machines after this many idle minutes; `0` disables idle cleanup |
| `max_lifetime_minutes` | Stop machines this many minutes after they start, even while in use; `0` disables the limit |
| `restricted` | Restricted Kubernetes/OpenShift defaults |
| `pod_security_context`, `container_security_context` | Kubernetes security context overrides |
| `podTemplate` | Kubernetes pod overrides, including volumes, scheduling, and health probes |

Service limits such as `MACHINES_MAX_CPU`, `MACHINES_MAX_MEMORY`, and `MACHINES_ALLOWED_IMAGES` constrain template settings.

### Network profiles

Define named profiles in the orchestrator's environment, then select one with
`"network_profile": "restricted"` in a template or the admin template editor:

```bash
MACHINES_NETWORK_PROFILES='{"restricted":{"docker_network":"terminals-restricted","kubernetes_labels":{"network-tier":"restricted"}}}'
```

Only the mapping for the active backend is required. Unknown names, missing
backend mappings, and invalid mapping types are rejected. Omit `network_profile`
to preserve existing networking behavior.

- **Docker:** `docker_network` selects existing named networks, with the same
  comma-separated sharding as `MACHINES_DOCKER_NETWORK`. Attach the orchestrator
  to every selected network so it can reach machines by name. Built-in modes
  such as `host` and `none` are not supported by profiles.
- **Kubernetes and Kubernetes operator:** `kubernetes_labels` adds pod labels for
  administrator-installed NetworkPolicies. Profile labels override conflicting
  pod-template labels; `openwebui.com/` and `app.kubernetes.io/` labels are reserved.
  The operator uses its existing `podTemplate` support, so no CRD change is needed.

Profiles select configuration; they do not create networks, firewall rules, or
NetworkPolicies. Configure Docker firewall rules or Kubernetes NetworkPolicies
and a CNI that enforces them to provide the intended access restrictions. A name
such as `restricted` alone does not enforce isolation. Allow the orchestrator to
reach port 8000 on each machine and permit DNS if needed. Use the restart operation below to apply
profile changes to existing machines.

### Apply changes to existing machines

Edit configuration with `PATCH /api/v1/machines/templates/{id}`. Changes take effect when an environment next starts. Restart stops the selected environment, preserves its files, and starts it again with the current template configuration:

```bash
curl -fsS -X POST http://localhost:3000/api/v1/machines/templates/data-science/restart \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"user_id":"demo-user"}'
```

For chat or model scope, also supply `chat_id` or `model_id`. Restart does not create an unused environment. Use `/stop` to stop it without starting it again, and `/start` to start it, creating it if needed. Scope and storage-mode changes are rejected while affected environments exist.

### Schedule file resets

Scheduled resets delete persisted machine files and refresh matching machines, including active ones. Configure them in the template editor or with:

```bash
curl -fsS -X PUT http://localhost:3000/api/v1/machines/templates/data-science/lifecycle \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"reset":{"schedule":"@weekly","timezone":"UTC"}}'
```

Schedules support one-time ISO datetimes, `@weekly`, `@monthly`, and five-field cron expressions. Docker clears the matching home directory. Kubernetes uses a reset pod to clear the matching PVC or environment subdirectory.

## Storage and persistence

Stopping or reaping a machine preserves its persisted files. Idle cleanup is disabled by default; enable it with `MACHINES_IDLE_TIMEOUT_MINUTES` or a template. A maximum lifetime, set with `MACHINES_MAX_LIFETIME_MINUTES` or a template, stops machines after a fixed time even while they are in use; the next request starts a fresh one with the same files. File resets and environment deletion are separate, destructive actions.

| Backend | Persisted machine files |
|---------|-------------------------|
| Docker | Bind-mounted under `MACHINES_DOCKER_DATA_DIR`, regardless of the template's `storage` field |
| Kubernetes | PVCs; defaults to `1Gi` per machine, with optional shared storage modes |
| Kubernetes operator | Dedicated PVCs; `storage` overrides the server default size |

### Docker paths

The quick start mounts the host's `./data` directory at `/app/data`:

| Host path | Container path | Contents |
|-----------|----------------|----------|
| `./data/terminals.db` | `/app/data/terminals.db` | SQLite database: templates and lifecycle state |
| `./data/terminals/` | `/app/data/terminals/` | Machine homes under `.terminals/<instance-id>` |

`MACHINES_DOCKER_HOST_DATA_DIR` tells the Docker daemon where those same machine files live on its host. It is optional when the orchestrator and daemon use the same path. Both paths must refer to the same storage so provisioning, resets, and deletion operate on the same files. The quick start assumes a local Docker daemon.

Back up the database and machine files before upgrades. Changing paths does not migrate existing data or change mounts on existing containers. Move data from its previous effective location before changing the configuration.

### Storage limits

On Kubernetes, `storage` sets the requested PVC size. On Docker it caps only the container's writable layer, and only when the storage driver supports `StorageOpt` (for example, overlay2 on XFS with `pquota`). Unsupported drivers log a warning and run without that limit. The bind-mounted `/home/user` directory is not covered by Docker's limit.

Machine files need a filesystem. Open WebUI's uploaded-file storage and Azure Blob cleanup do not manage them. For Azure, use Azure Files, managed disks, or Kubernetes PVCs for machine homes.

### Delete an environment's files

Deleting an Open WebUI account does not automatically delete machine storage. Inspect a template's tracked instances with `GET /api/v1/machines/templates/{id}/instances` (or all instances with `GET /api/v1/machines`), then delete the specific template/context:

```bash
curl -fsS -X DELETE http://localhost:3000/api/v1/machines/templates/data-science/instance \
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
| `kubernetes` | Machines should manage Pods, PVCs, and Services directly, without a CRD |

From a checkout of this repository, install the operator:

```bash
kubectl apply -f manifests/terminal-crd.yaml
kubectl apply -f manifests/operator-deployment.yaml
```

These commands install the operator, not the Machines API service. Deploy `ghcr.io/open-webui/terminals:latest` separately with:

```yaml
env:
  - name: MACHINES_BACKEND
    value: kubernetes-operator
  - name: MACHINES_KUBERNETES_NAMESPACE
    value: terminals
  - name: MACHINES_API_KEY
    valueFrom:
      secretKeyRef:
        name: terminals
        key: api-key
```

Create the referenced Secret, mount persistent storage at `/app/data` for SQLite, and give the service account the backend's required Kubernetes permissions. The operator RBAC is in [operator-deployment.yaml](manifests/operator-deployment.yaml). Expose the Machines service on port `3000` to Open WebUI. Add `"storage":"5Gi"` to operator templates to request that PVC size.

For direct Kubernetes provisioning, use `MACHINES_BACKEND=kubernetes`; no operator or CRD is required. For restricted OpenShift clusters, follow [OpenShift deployment](docs/openshift.md).

## Authentication

The quick start uses a static `MACHINES_API_KEY` for both the admin UI and Open WebUI's Bearer connection. Keep it on trusted servers: API-key requests trust the supplied `X-User-Id` to select the user.

Managed execution uses the server key. WebUI checks access and forwards verified context; user JWTs do not authorize managed template requests.

Tool discovery at `/openapi.json` uses the server API key and the same verified template, user, and scope context as execution. It uses that instance; there is no separate discovery instance.

`terminals serve` generates and prints a key if none is configured. That generated key changes when the service process restarts. Configure a persistent API key for deployments; managed execution requires it even when running the ASGI app directly.

## Configuration

Every `MACHINES_*` setting also accepts its `TERMINALS_*` name. You can mix prefixes and migrate one setting at a time. Process environment variables take precedence over the service’s `.env` file. Within either source, `MACHINES_*` takes precedence, including an explicitly empty value. The Docker shortcuts `NETWORK` and `DATA_DIR` work with either prefix.

### Common

| Variable | Default | Description |
|----------|---------|-------------|
| `MACHINES_BACKEND` | `docker` | `docker`, `kubernetes`, or `kubernetes-operator` |
| `MACHINES_API_KEY` | *(auto-generated)* | Bearer token for API auth |
| `MACHINES_HOST` | `0.0.0.0` | Orchestrator API bind host |
| `MACHINES_PORT` | `3000` | Orchestrator API bind port |
| `MACHINES_WORKERS` | `1` | Uvicorn worker process count. Docker workers adopt existing containers by deterministic name instead of replacing them. |
| `MACHINES_ENABLE_UI` | `true` | Serve the built-in minimal admin UI at `/`. Set to `false` for API-only deployments. |
| `MACHINES_IMAGE` | `ghcr.io/open-webui/open-terminal:latest` | Default Machine (Open Terminal) container image |
| `MACHINES_IMAGE_PULL_POLICY` | | `Always`, `IfNotPresent`, or `Never`, for every backend. Unset pulls images tagged `:latest` or untagged on every start and other tags only when missing, matching Kubernetes. |
| `MACHINES_IMAGE_PULL_INTERVAL_SECONDS` | `3600` | Docker only: how long a pulled image is used before `Always` checks the registry again. If the registry can't be reached, the local copy is used. |
| `MACHINES_NETWORK_PROFILES` | `{}` | JSON object mapping profile names to `docker_network` and/or `kubernetes_labels` configuration |
| `MACHINES_MAX_CPU` | | Hard cap on CPU per machine container/pod |
| `MACHINES_MAX_MEMORY` | | Hard cap on memory per machine container/pod |
| `MACHINES_MAX_STORAGE` | | Storage cap. Kubernetes enforces this with PVC sizes; Docker support depends on the storage driver and does not quota the bind-mounted `/home/user`. |
| `MACHINES_ALLOWED_IMAGES` | | Comma-separated list of allowed image patterns |
| `MACHINES_IDLE_TIMEOUT_MINUTES` | `0` | Minutes of inactivity before machines are torn down. `0` disables idle cleanup. |
| `MACHINES_MAX_LIFETIME_MINUTES` | `0` | Minutes after start before machines are torn down, even while in use. `0` disables the limit. |
| `MACHINES_IDLE_CLEANUP_TIMEOUT_SECONDS` | `120` | Timeout for each teardown/reset call during idle cleanup |
| `MACHINES_DATABASE_URL` | `sqlite+aiosqlite:////app/data/terminals.db` in the image | SQLAlchemy database URL. Source installs use `data/terminals.db` next to the package directory. PostgreSQL is optional. |
| `MACHINES_LOG_LEVEL` | `INFO` | Minimum orchestrator log level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. On Docker, `WARNING` or higher disables child container Docker logs because Machine (Open Terminal) does not expose a log-level env var. |
| `MACHINES_STATUS_CACHE_TTL` | `30` | Seconds a confirmed-running container status is trusted before re-inspecting it via the backend. `0` re-checks on every request. |
| `MACHINES_WS_COMPRESSION` | `false` | Enable permessage-deflate on proxied WebSocket traffic. Leave off unless clients connect over slow links. |
| `MACHINES_ACCESS_LOG` | `false` | Log every HTTP request. Off by default because per-request logging is expensive at high request rates. |
| `MACHINES_REPLAY_BODY_LIMIT` | | Maximum proxied request body bytes buffered for retry. Unset, `none`, `null`, or `unlimited` means no size cap. |
| `MACHINES_PROXY_CONNECT_TIMEOUT_SECONDS` | `10` | Timeout for opening upstream proxy connections to machine containers |
| `MACHINES_PROXY_READ_TIMEOUT_SECONDS` | `360` | Timeout while waiting for upstream proxy responses. Set this above the maximum `/execute?wait` value for Machine (Open Terminal). |
| `MACHINES_PROXY_WRITE_TIMEOUT_SECONDS` | `300` | Timeout while sending proxied request bodies upstream |
| `MACHINES_PROXY_POOL_TIMEOUT_SECONDS` | `300` | Timeout while waiting for an upstream proxy connection from the pool |
| `MACHINES_PROVISION_TIMEOUT_SECONDS` | `120` | Timeout waiting for machine readiness on the `kubernetes-operator` backend |

### Docker

| Variable | Default | Description |
|----------|---------|-------------|
| `MACHINES_DOCKER_HOST` | `127.0.0.1` | Host/IP that the orchestrator uses to reach published machine container ports |
| `MACHINES_DOCKER_NETWORK` | | Docker network for machine containers. Use a comma-separated list to shard machines across multiple Docker bridge networks. |
| `MACHINES_DOCKER_MOUNTS` | | JSON array of extra bind mounts with `source`, `target`, and optional `readOnly` |
| `MACHINES_DOCKER_DATA_DIR` | `data/terminals` (`/app/data/terminals` in the image) | Workspace directory accessible to the orchestrator, including file resets |
| `MACHINES_DOCKER_HOST_DATA_DIR` | | Optional absolute Docker-host path to the same workspace directory. Unset uses `MACHINES_DOCKER_DATA_DIR`. |

### Kubernetes

| Variable | Default | Description |
|----------|---------|-------------|
| `MACHINES_KUBERNETES_NAMESPACE` | `terminals` | Namespace for machine pods, services, PVCs, and CRDs |
| `MACHINES_KUBERNETES_IMAGE` | | Kubernetes container image override. Defaults to `MACHINES_IMAGE`. |
| `MACHINES_KUBERNETES_STORAGE_CLASS` | | Storage class for machine PVCs. Empty uses the cluster default. |
| `MACHINES_KUBERNETES_STORAGE_SIZE` | `1Gi` | Default PVC size for the direct `kubernetes` backend; both Kubernetes backends use this default when template `storage` is omitted |
| `MACHINES_KUBERNETES_STORAGE_MODE` | `per-user` | Direct Kubernetes storage mode: `per-user`, `shared`, or `shared-rwo`; the operator creates dedicated PVCs |
| `MACHINES_KUBERNETES_SERVICE_TYPE` | `ClusterIP` | Service type for machine services |
| `MACHINES_KUBERNETES_KUBECONFIG` | | Kubeconfig path. Empty uses in-cluster config. |
| `MACHINES_KUBERNETES_LABELS` | | Extra labels for machine resources, as `k=v,k2=v2` |
| `MACHINES_KUBERNETES_RESTRICTED` | `false` | Enable restricted Kubernetes/OpenShift pod defaults globally |
| `MACHINES_KUBERNETES_POD_SECURITY_CONTEXT` | | JSON pod security context merged into machine pods |
| `MACHINES_KUBERNETES_CONTAINER_SECURITY_CONTEXT` | | JSON container security context merged into machine containers |
| `MACHINES_KUBERNETES_NODE_SELECTOR` | | Node selector for machine and reset pods, as JSON or `k=v,k2=v2` |
| `MACHINES_KUBERNETES_TOLERATIONS` | | JSON array of tolerations for machine and reset pods |

### Kubernetes operator

| Variable | Default | Description |
|----------|---------|-------------|
| `MACHINES_KUBERNETES_CRD_GROUP` | `openwebui.com` | `Terminal` CRD API group |
| `MACHINES_KUBERNETES_CRD_VERSION` | `v1alpha1` | `Terminal` CRD API version |

These tables are the public environment-variable surface. Compatibility-only
settings may still be accepted by the code but are intentionally omitted.

By default, known-size proxied request bodies are buffered so retry behavior is preserved. Set `MACHINES_REPLAY_BODY_LIMIT` to stream request bodies above that byte limit instead of buffering them in orchestrator memory. Chunked uploads are always streamed one-shot and are not retried.

## Advanced configuration

### Extra mounts


Docker deployments can use `MACHINES_DOCKER_MOUNTS` to mount shared data into
every spawned machine container:

```bash
MACHINES_DOCKER_MOUNTS='[
  {"source": "/srv/datasets", "target": "/mnt/datasets", "readOnly": true},
  {"source": "/srv/shared-work", "target": "/workspace/shared", "readOnly": false}
]'
```

`source` is an absolute path on the Docker daemon host. `target` is the absolute
container path. `readOnly` defaults to `true`. Do not mount over `/home/user`;
Machine (Open Terminal) expects that directory to stay writable.

Kubernetes deployments do not use `MACHINES_DOCKER_MOUNTS`. Use the existing
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
boundary if machine containers are privileged or can access the Docker socket.

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

Omitted probes keep their existing defaults. Restart existing machines to apply
the updated template on their next access.

### Docker network scaling

For larger Docker deployments, create multiple bridge networks and set `MACHINES_DOCKER_NETWORK` to a comma-separated list. The service assigns each template/context to a stable network. Attach the orchestrator container to every listed network so it can reach machines by name.

## Troubleshooting

| Problem | What to check |
|---------|---------------|
| Admin UI shows `401` | Paste the Machines API key, then click Connect. Use the saved key, not an Open WebUI user token. |
| Open WebUI cannot connect | Use `http://terminals:3000` on the shared Docker network. Inside a container, `localhost` refers to that container. |
| First machine is slow or fails | Check `docker logs terminals`; the configured machine image must be available or pullable. |
| Files disappear after recreating the service | Check the `/app/data` bind mount and both Docker data paths. Keep the same host directory across recreations. |
| Kubernetes machine files disappear after idle cleanup | Check that the environment PVC still exists and `/home/user` is mounted from it. |
| Environment deletion returns `409` | The workload may still be stopping. Check the returned error and retry; files are not deleted while the workload remains. |
| UI is unavailable in a source install | Build the frontend and ensure `MACHINES_ENABLE_UI` is not `false`. |

## Development

Use Python 3.11+ and Node.js 22.12+:

```bash
git clone https://github.com/open-webui/terminals.git
cd terminals
python -m venv .venv
source .venv/bin/activate
pip install -e .
npm --prefix machines/frontend ci
npm --prefix machines/frontend run build
terminals serve
```

Docker must be available when using the default backend. The command prints the generated API key; open `http://localhost:3000` to use the admin UI. During frontend development, run `npm --prefix machines/frontend run dev`; its API proxy targets the backend on port `3000`.

## License

Free for personal, non-commercial use. Commercial or organizational production use requires an Open WebUI Enterprise License. All rights reserved. See [LICENSE](LICENSE).
