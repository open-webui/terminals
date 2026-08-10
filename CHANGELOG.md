# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.1] - 2026-08-09

### Added
- Added Docker extra mounts. Operators can mount shared folders, datasets, and team workspaces into every spawned terminal with `TERMINALS_DOCKER_MOUNTS`.
- Added read-only mount support for Docker extra mounts. Read-only is the default, so shared datasets are protected unless an operator explicitly allows writes.
- Added clear Docker and Kubernetes mount guidance. Docker uses `TERMINALS_DOCKER_MOUNTS`; Kubernetes uses policy `podTemplate` volumes and volume mounts.

### Changed
- Renamed Docker-only settings so their purpose is clear: use `TERMINALS_DOCKER_NETWORK` and `TERMINALS_DOCKER_DATA_DIR`. The old names still work for existing deployments.
- Split the configuration docs into common, Docker-only, Kubernetes-only, and Kubernetes-operator-only sections.
- Kubernetes now uses `TERMINALS_IMAGE` as the normal image setting, matching Docker. The older Kubernetes image override still works for existing deployments.
- Documented `TERMINALS_OPEN_WEBUI_URL` as an advanced Open WebUI JWT mode that requires session auth from Open WebUI.

### Fixed
- Blocked Docker extra mounts from replacing important container paths such as `/home/user`, `/etc`, `/usr`, and `/var`.
- Blocked duplicate or nested Docker mount targets so one mount cannot hide another.

## [0.2.0] - 2026-08-08

### Added
- Added support for separate terminals per saved chat and per automation run. Open WebUI `0.11.1` or newer is required to use this from Open WebUI.
- Added storage and restart recovery for shared terminals, chat terminals, and automation terminals across Docker, Kubernetes, and Kubernetes operator deployments.
- Added Kubernetes pod customization for cluster requirements like Kata Containers, sidecars, extra mounts, volumes, service accounts, labels, and annotations.
- Added context targeting to admin refresh and stop actions, so admins can refresh or stop one shared, chat, or automation terminal without touching the others for the same user and policy.

### Changed
- Existing Open Terminal and Open WebUI connections still use one shared terminal unless Open WebUI sends a chat or automation context.
- Missing, empty, and `default` context values all use the shared terminal.
- Proxy timeout settings are configurable, and the default read timeout now gives Open Terminal `/execute?wait=300` enough room to finish.

### Fixed
- Fixed long-running `/execute` calls near the old 300 second proxy limit. They get more time by default, and Terminals returns a clear timeout response if the terminal still does not answer.

## [0.1.0] - 2026-07-21

### Added
- Large Docker installs can now split terminals across several Docker networks by setting `TERMINALS_NETWORK` to a comma-separated list, so every active terminal no longer has to sit on the same network.
- The README now gives the exact setup for this: create multiple Docker networks, list them in `TERMINALS_NETWORK`, and attach the orchestrator to each one.

## [0.0.8] - 2026-07-20

### Added
- Added `TERMINALS_IDLE_CLEANUP_TIMEOUT_SECONDS` so deployments can choose how long idle cleanup waits for one terminal stop or reset before trying again later.

### Changed
- After a Terminals restart, recovered Docker and Kubernetes terminals now keep their policy settings instead of falling back to the global defaults.
- After a Terminals restart, recovered terminals keep their last recorded activity time instead of being treated as newly active.
- New Kubernetes terminal pods now save the real policy id, so recovered pods are matched to the right policy even when the policy label is shortened.

### Fixed
- Fixed idle cleanup getting stuck behind one slow terminal stop or reset. Timed-out cleanup is left tracked and retried on the next sweep.

## [0.0.7] - 2026-07-20

### Added
- Added `TERMINALS_WORKERS` and `--workers` so deployments can run multiple Terminals server processes.
- Added `TERMINALS_REPLAY_BODY_LIMIT`. Leave it unset for unlimited retry buffering, or set a byte limit when large uploads should stream instead of staying in server memory.

### Changed
- Docker mode now adopts an already-running user terminal when another worker finds it, instead of deleting that terminal and starting over.
- Docker mode can now quiet hosted terminal container logs when orchestrator logging is set to `WARNING`, `ERROR`, or `CRITICAL`.
- Worker processes now share terminal activity heartbeats, so active sessions are not mistaken for idle sessions just because another worker served the request.
- Scheduled policy resets now refresh matching running terminals, so a browser tab left open all day does not block scheduled file cleanup.
- Kubernetes operator deployments now group activity updates instead of sending one for every request, which lowers cluster API load while keeping active sessions protected.

### Fixed
- Fixed retry behavior for ordinary proxied requests so requests can be replayed after a terminal is replaced. Chunked uploads still stream once because they cannot be replayed safely.
- Fixed Docker multi-worker startup conflicts where two workers could fight over the same user's terminal container.
- Fixed stale activity records being left behind after refreshes, resets, missing terminals, and Docker restart recovery.

## [0.0.6] - 2026-07-19

### Added
- Added a policy rollout action in the admin UI. It refreshes idle terminals for one policy and reports exactly how many were refreshed and how many active terminals were skipped.

### Changed
- Reworked the admin UI into a cleaner, denser light workspace with fewer borders, tighter rows, clearer policy actions, and a policy editor that keeps Save visible while scrolling.
- Made busy terminal traffic lighter to serve by avoiding repeated health checks, repeated Open WebUI login checks, and repeated Kubernetes status writes during normal proxy use.
- Split proxy connection pools by terminal instance so heavy traffic to one terminal does not slow down unrelated terminal sessions.
- Turned off access logs and WebSocket compression by default to reduce noisy runtime overhead.
- Documented the new runtime settings for status caching, token caching, WebSocket compression, and access logs.

### Fixed
- Fixed packaged installs and Docker images so the built admin UI is included in the Python package. Visiting the root page now loads the UI instead of falling through to the proxy and returning `401 Missing Authorization header`.
- Fixed policy save and delete flows so cached policy and tool details are cleared immediately after a change.
- Fixed HTTP and WebSocket retries so, when a terminal is starting or has been replaced, the proxy re-checks the current terminal before retrying instead of retrying an old address.
- Fixed the active WebSocket connection count so failed connection attempts no longer leave the admin status count permanently too high.
- Fixed retry failure handling so HTTP and WebSocket requests return a clean terminal-unreachable error when a terminal cannot be resolved during retry.

## [0.0.5] - 2026-07-09

### Added
- Added Kubernetes node selector and toleration overrides for terminal and reset pods.

## [0.0.4] - 2026-06-29

### Added
- Added a minimal admin UI for viewing terminal status, active sessions, and policies.
- Added policy lifecycle support, including scheduled resets and lifecycle state tracking.
- Added OpenShift-focused security context controls and deployment documentation.
- Added frontend build packaging to the server Docker image.
- Added terminal environment propagation for system prompts and resource metadata.
- Added configurable server and operator log levels.

### Fixed
- Fixed Docker backend storage limit handling with a best-effort fallback when the host driver cannot enforce quotas.
- Fixed stale proxy connection handling by retrying once after keep-alive failures.
- Fixed Kubernetes and operator provisioning paths to pass effective policy environment values consistently.

## [0.0.1] - 2026-04-02

### Added
- Multi-tenant terminal orchestrator with Docker and Kubernetes backends.
- Kubernetes operator for terminal custom resource management.
- CLI interface for managing terminals.
- Docker build workflows for orchestrator and operator images (multi-arch: amd64/arm64).
