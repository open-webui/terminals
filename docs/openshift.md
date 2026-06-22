# OpenShift Deployment

Terminals can run on OpenShift as a restricted per-user terminal sandbox.

OpenShift restricted SCC does not support the full rootful agent devbox model. Use prebuilt terminal images and let OpenShift provide the isolation boundary through one pod per user.

## What Works

- One Open Terminal pod per user and policy.
- Persistent user files through PVCs.
- Policy-selected images, env vars, CPU, memory, storage, and idle timeout.
- Refresh and scheduled reset of persisted terminal files.
- File browser, command execution, notebooks, and tools that are already in the image.

## What Does Not Work Under Restricted SCC

- Runtime OS package installs through `OPEN_TERMINAL_PACKAGES`.
- Runtime global package installs through `OPEN_TERMINAL_PIP_PACKAGES` or `OPEN_TERMINAL_NPM_PACKAGES`.
- `sudo`, dynamic Linux user creation, or `OPEN_TERMINAL_MULTI_USER=true`.
- Docker socket access, Docker-in-Docker, or host Docker control.
- The iptables/dnsmasq egress firewall from `OPEN_TERMINAL_ALLOWED_DOMAINS`.

Build a custom Open Terminal image when users need additional tools.

## Database

PostgreSQL is not required.

Terminals uses SQLite by default:

```text
sqlite+aiosqlite:///.../data/terminals.db
```

Mount persistent storage at `/app/data` for the Terminals service if you use the default SQLite database. Use `TERMINALS_DATABASE_URL` only when you want an external database.

## Install The Operator

Install the CRD and operator:

```bash
oc apply -f manifests/terminal-crd.yaml
oc apply -f manifests/operator-deployment.yaml
```

The operator deployment is designed for non-root execution and does not require privileged or anyuid SCC.

## Deploy Terminals

Run the Terminals service with the operator backend and restricted mode:

```yaml
env:
  - name: TERMINALS_BACKEND
    value: kubernetes-operator
  - name: TERMINALS_KUBERNETES_NAMESPACE
    value: terminals
  - name: TERMINALS_KUBERNETES_RESTRICTED
    value: "true"
```

If you use the default SQLite database, mount a PVC at `/app/data`.

## Example Policy

Use the OpenShift-compatible Open Terminal image:

```bash
curl -X PUT https://terminals.example.com/api/v1/policies/openshift \
  -H "Authorization: Bearer $TERMINALS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "image": "ghcr.io/open-webui/open-terminal:openshift",
    "restricted": true,
    "storage": "5Gi",
    "storage_mode": "per-user",
    "cpu_limit": "1",
    "memory_limit": "1Gi",
    "env": {
      "OPEN_TERMINAL_FILE_BROWSER_ROOT": "home"
    }
  }'
```

Restricted mode applies these defaults unless you override them:

```json
{
  "pod_security_context": {
    "runAsNonRoot": true,
    "seccompProfile": {
      "type": "RuntimeDefault"
    }
  },
  "container_security_context": {
    "allowPrivilegeEscalation": false,
    "capabilities": {
      "drop": ["ALL"]
    },
    "runAsNonRoot": true
  }
}
```

Do not set `OPEN_TERMINAL_ALLOWED_DOMAINS` in restricted policies. Use cluster network policy for egress control on OpenShift.

## Custom Images

For OpenShift, install tools at image build time:

```dockerfile
FROM ghcr.io/open-webui/open-terminal:openshift

USER 0
RUN apt-get update && apt-get install -y --no-install-recommends ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && chgrp -R 0 /home/user /app \
    && chmod -R g=u /home/user /app
USER 1001
```

Then point the policy `image` field at your built image and refresh affected terminals.
