# Terminals

Multi-tenant terminal orchestrator for [Open Terminal](https://github.com/open-webui/open-terminal).

Provisions and manages isolated Open Terminal containers per user, with tenant scoping and a single authenticated API entry point.

## Kubernetes Controller

The Kubernetes Controller automatically provisions and manages per-user [Open Terminal](https://github.com/open-webui/open-terminal) pods inside a Kubernetes cluster.

### Architecture

```
Browser → Open WebUI → Orchestrator → Controller → Per-user Terminal Pod
```

| Component | Description |
|---|---|
| **Terminal CRD** (`terminals.open-webui.com/v1alpha1`) | Declares a per-user terminal instance as a Kubernetes custom resource. |
| **Controller** (`controller.py`, kopf) | Watches Terminal CRDs and reconciles a Pod, Service, Secret, and PVC for each one. Culls idle terminals after a configurable timeout. |
| **Orchestrator** (`app.py`, FastAPI) | Single authenticated entry point. Receives requests from Open WebUI, ensures the user's Terminal CRD exists, waits for the pod to become ready, and proxies HTTP/WebSocket traffic to it. Also serves a cached `/openapi.json` so Open WebUI can discover terminal tools. |

### Prerequisites

- Kubernetes cluster (v1.24+)
- `kubectl` configured for the target cluster
- Container images built and accessible (see below)
- Open WebUI configured with `TERMINAL_SERVER_CONNECTIONS`

### Building Images

```bash
# From the terminals/ directory
docker build -f Dockerfile.controller -t terminals-controller:latest .
docker build -f Dockerfile.orchestrator -t terminals-orchestrator:latest .
```

### Manual Deployment

1. **Apply the CRD and RBAC resources:**

```bash
kubectl apply -f manifests/crd.yaml
kubectl apply -f manifests/rbac.yaml
```

2. **Deploy the controller and orchestrator** (adjust image names/namespaces as needed):

```yaml
# controller deployment
apiVersion: apps/v1
kind: Deployment
metadata:
  name: terminals-controller
  namespace: open-webui
spec:
  replicas: 1
  selector:
    matchLabels:
      app: terminals-controller
  template:
    metadata:
      labels:
        app: terminals-controller
    spec:
      serviceAccountName: terminals-controller
      containers:
        - name: controller
          image: terminals-controller:latest
          env:
            - name: TERMINAL_NAMESPACE
              value: open-webui
---
# orchestrator deployment + service
apiVersion: apps/v1
kind: Deployment
metadata:
  name: terminals-orchestrator
  namespace: open-webui
spec:
  replicas: 1
  selector:
    matchLabels:
      app: terminals-orchestrator
  template:
    metadata:
      labels:
        app: terminals-orchestrator
    spec:
      serviceAccountName: terminals-controller
      containers:
        - name: orchestrator
          image: terminals-orchestrator:latest
          ports:
            - containerPort: 8080
          env:
            - name: TERMINAL_NAMESPACE
              value: open-webui
            - name: TERMINAL_API_KEY
              value: "your-secret-key"
---
apiVersion: v1
kind: Service
metadata:
  name: terminals-orchestrator
  namespace: open-webui
spec:
  selector:
    app: terminals-orchestrator
  ports:
    - port: 8080
      targetPort: 8080
```

3. **Configure Open WebUI** to point at the orchestrator:

```
TERMINAL_SERVER_CONNECTIONS='[{"url":"http://terminals-orchestrator:8080","key":"your-secret-key"}]'
```

### Helm Chart (not released to main yet)

A Helm sub-chart will be available in the Open WebUI Helm Charts and is wired into the Open WebUI parent chart as an optional dependency. Enable it in your Open WebUI values:

```yaml
terminals:
  enabled: true
  orchestrator:
    apiKey: "your-secret-key" # TODO: Update to secret ref
```

### Configuration

The orchestrator and controller are configured via environment variables:

| Variable | Default | Description |
|---|---|---|
| `TERMINAL_NAMESPACE` | `open-webui` | Namespace for Terminal CRDs and pods |
| `TERMINAL_DEFAULT_IMAGE` | `ghcr.io/open-webui/open-terminal:latest` | Container image for terminal pods |
| `TERMINAL_DEFAULT_CPU_REQUEST` | `100m` | CPU request per terminal pod |
| `TERMINAL_DEFAULT_MEMORY_REQUEST` | `256Mi` | Memory request per terminal pod |
| `TERMINAL_DEFAULT_CPU_LIMIT` | `1` | CPU limit per terminal pod |
| `TERMINAL_DEFAULT_MEMORY_LIMIT` | `1Gi` | Memory limit per terminal pod |
| `TERMINAL_DEFAULT_IDLE_TIMEOUT` | `30` | Minutes of inactivity before a terminal is culled |
| `TERMINAL_DEFAULT_PERSISTENCE_ENABLED` | `true` | Attach a PVC to each terminal pod |
| `TERMINAL_DEFAULT_PERSISTENCE_SIZE` | `1Gi` | PVC size per terminal |
| `TERMINAL_DEFAULT_STORAGE_CLASS` | _(cluster default)_ | StorageClass for terminal PVCs |
| `TERMINAL_API_KEY` | _(required)_ | Shared secret between Open WebUI and the orchestrator |

## License

[Open WebUI Enterprise License](LICENSE) — see LICENSE for details.
