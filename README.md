# Terminals

> [!NOTE]
> This project is **actively under development**. APIs, configuration, and behavior may change between releases.

Per-user [Open Terminal](https://github.com/open-webui/open-terminal) orchestration for Docker and Kubernetes.

Terminals gives every Open WebUI user their own isolated container — with separate credentials, resource limits, and network rules. It handles the full lifecycle automatically: spinning up containers when a user connects, proxying traffic, enforcing limits, and cleaning up when they're done.

```
Open WebUI  →  Terminals service  →  per-user containers
               (this project)        (Open Terminal images)
```

> [!IMPORTANT]
> **Production use requires an [Open WebUI Enterprise License](LICENSE) with Terminals access.** Contact the Open WebUI team to get started.

## Quick Start

The fastest way to get running is with Docker. Terminals will manage sibling containers through the Docker socket.

### Docker (recommended for single-node)

```bash
docker run -p 3000:3000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v $(pwd)/data:/app/data \
  terminals
```

**Prerequisites:** Docker running on the host.

### Kubernetes with Agent Sandbox (recommended for clusters)

For Kubernetes deployments, Terminals builds on the upstream
[Agent Sandbox](https://github.com/kubernetes-sigs/agent-sandbox) project (SIG Apps).
Each user+policy maps to a single `Sandbox` custom resource; the agent-sandbox
controller reconciles it into a Pod, a headless Service (a stable `serviceFQDN`),
and a PersistentVolume when a workspace is requested. Idle terminals are
**suspended** (`operatingMode: Suspended`, scale-to-zero with storage and identity
preserved) and resumed on the next request.

Workspace persistence: the per-user `PersistentVolumeClaim` is created from the
Sandbox's `volumeClaimTemplates` and owned by the Sandbox. Suspending keeps the
Sandbox object, so `/workspace` data survives idle and resume. **Tearing a terminal
down deletes the Sandbox and its PVC** (workspace data is removed) — idle reaping uses
suspend, not teardown, so normal inactivity never destroys data.

```bash
# 1. Install the agent-sandbox controller + extensions (pin a release version)
export VERSION="v0.5.0"  # see https://github.com/kubernetes-sigs/agent-sandbox/releases
kubectl apply -f https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${VERSION}/manifest.yaml
kubectl apply -f https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${VERSION}/extensions.yaml

# 2. Grant the Terminals service access to the Sandbox CRDs
kubectl apply -f manifests/sandbox-rbac.yaml
```

Set `TERMINALS_BACKEND=kubernetes-sandbox` when deploying the Terminals service
(with `serviceAccountName: terminals`). For stronger isolation of user code, set
`TERMINALS_SANDBOX_RUNTIME_CLASS=gvisor` (or `kata-qemu`) once the runtime is
installed on your nodes.

> [!NOTE]
> Agent Sandbox is a young upstream project — this backend targets `v1beta1` (v0.5.x);
> pin a release version and track changes. Two trade-offs to be aware of: the per-user
> API key lives as a plaintext env value in the `Sandbox` pod template (rely on RBAC +
> etcd encryption-at-rest), and **idle tracking is in-memory** in the Terminals process
> (run a single Terminals replica, or expect idle to be tracked per-replica). Warm pools
> are intentionally not used — they are mutually exclusive with per-user API keys (an
> env-injecting claim bypasses the pool), so first connection pays pod start-up (the
> image is cached on the node after the first pull).
>
> These last two are things the backend self-manages **because the controller does not
> yet**. Both are on the [upstream roadmap](https://github.com/kubernetes-sigs/agent-sandbox/blob/main/roadmap.md)
> (*Auto Suspend/Resume*, *Scale to Zero*, *Sandbox/Pod Identity Association*); the
> backend is pinned to `v1beta1` specifically so we can drop these shims as they land.

### Kubernetes with the bundled operator (custom CRD)

The operator backend reconciles a custom `Terminal` CRD with a self-hosted
[Kopf](https://kopf.readthedocs.io/) operator. Use this if you prefer a
self-contained controller over the upstream Agent Sandbox project.

```bash
# 1. Install the Terminal CRD and the operator (Deployment + RBAC)
kubectl apply -f manifests/terminal-crd.yaml
kubectl apply -f manifests/operator-deployment.yaml
```

Set `TERMINALS_BACKEND=kubernetes-operator` when deploying the Terminals service.

### From source (development)

```bash
pip install -e .
terminals serve
```

## Choosing a Backend

| Backend | Best for | How it works |
|---------|----------|-------------|
| `docker` | Single-node, local dev | One container per user via Docker socket |
| `kubernetes-sandbox` | Production K8s clusters (upstream CRD) | One [Agent Sandbox](https://github.com/kubernetes-sigs/agent-sandbox) `Sandbox` per user; suspend/resume on idle |
| `kubernetes-operator` | Production K8s clusters (self-hosted operator) | Custom `Terminal` CRD reconciled by the bundled Kopf operator |
| `kubernetes` | K8s without CRDs | Direct Pod + PVC + Service per user (you manage resources) |

> Both `kubernetes-sandbox` and `kubernetes-operator` are fully supported. The sandbox backend builds on the upstream Agent Sandbox controller; the operator backend uses our own `Terminal` CRD. Pick one per deployment via `TERMINALS_BACKEND`.

Set the backend with `TERMINALS_BACKEND` (defaults to `docker`).

## Policies

Policies let you define different environments — for example, a "data-science" environment with extra CPU and specific Python packages, or a "sandbox" environment with restricted network access.

Without any policies, Terminals uses the defaults from your configuration. Once you're ready to customize, manage policies through the REST API:

```bash
# Create a "data-science" policy
curl -X PUT http://localhost:3000/api/v1/policies/data-science \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "image": "ghcr.io/open-webui/open-terminal:python-ds",
    "cpu_limit": "2",
    "memory_limit": "4Gi",
    "env": {
      "OPENAI_API_KEY": "sk-proj-...",
      "OPEN_TERMINAL_ALLOWED_DOMAINS": "*.pypi.org,github.com"
    },
    "idle_timeout_minutes": 30
  }'
```

Route requests through a policy by adding `/p/{policy_id}/` to the URL:

```bash
curl -X POST http://localhost:3000/p/data-science/execute \
  -H "Authorization: Bearer $API_KEY" -H "X-User-Id: user-123" \
  -H "Content-Type: application/json" \
  -d '{"command": "echo hello"}'
```

### Policy fields

| Field | Type | Description |
|-------|------|-------------|
| `image` | string | Container image to use |
| `env` | dict | Environment variables passed to the container |
| `cpu_limit` | string | Max CPU (e.g. `"2"`) |
| `memory_limit` | string | Max memory (e.g. `"4Gi"`) |
| `storage` | string | Persistent volume size (omit for ephemeral storage) |
| `storage_mode` | string | `per-user`, `shared`, or `shared-rwo` |
| `idle_timeout_minutes` | int | Minutes of inactivity before the container is cleaned up |

## Configuration

All settings are configured through environment variables prefixed with `TERMINALS_`, or via a `.env` file.

| Variable | Default | Description |
|----------|---------|-------------|
| `TERMINALS_BACKEND` | `docker` | `docker`, `kubernetes`, `kubernetes-operator`, or `kubernetes-sandbox` |
| `TERMINALS_API_KEY` | *(auto-generated)* | Bearer token for API auth |
| `TERMINALS_IMAGE` | `ghcr.io/open-webui/open-terminal:latest` | Default container image |
| `TERMINALS_MAX_CPU` | | Hard cap on CPU per container |
| `TERMINALS_MAX_MEMORY` | | Hard cap on memory per container |
| `TERMINALS_MAX_STORAGE` | | Hard cap on storage per container |
| `TERMINALS_ALLOWED_IMAGES` | | Comma-separated list of allowed image patterns |
| `TERMINALS_KUBERNETES_STORAGE_MODE` | `per-user` | `per-user`, `shared`, or `shared-rwo` |
| `TERMINALS_SANDBOX_RUNTIME_CLASS` | | RuntimeClass for sandbox isolation, e.g. `gvisor` or `kata-qemu` |

See [`config.py`](terminals/config.py) for the full list.

## Authentication

| Mode | How to enable |
|------|---------------|
| **API Key** | Set `TERMINALS_API_KEY` to a static token |
| **Open (dev only)** | Leave unset — no auth, for local development only |

## License

[Open WebUI Enterprise License](LICENSE)
