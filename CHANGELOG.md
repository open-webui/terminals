# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] - 2026-11-10 — BREAKING CHANGES

> [!WARNING]
> **Breaking upgrade: manual migration required.** Existing installations cannot upgrade by replacing the Terminals image alone. This release changes the database schema, storage paths, and saved terminal selections, and **does not provide automatic migration**. Back up both databases and all terminal files before following the [migration steps below](#moving-to-040-manually). Upgrade Open WebUI as part of the same migration, and retain the original databases and storage until you have verified the migrated files, permissions, and terminal selections.

**Requires Open WebUI 0.13.4 or later.**

0.4.0 brings terminal settings and sharing rules into one **template**, selected directly in Open WebUI. Previously, you had to check both the policy and the connection settings to know which workspace someone would get. Now that choice lives on the template.

For example, a team can use one Python template with a separate workspace for each person. Everyone keeps their own files, but you only maintain one set of settings. Change the image or resource limits once, and each workspace uses those settings the next time it starts.

When people need to work together, choose **Chat** scope: everyone allowed to continue that chat uses the same terminal and files. Previously, sharing a chat could still leave each person in a different terminal. **Model** scope keeps a workspace available across chats using the same model or preset. For two independent experiments, create two templates with scope **None**.

You can create, configure, and share these templates from WebUI's terminal selector. WebUI checks access, and Terminals manages the workspaces. Policies become templates as part of this change; the scope options below replace the old allocation flags.

### Scopes

Each template has one scope. A workspace includes both the running terminal and its saved files.

| Scope | What happens |
| --- | --- |
| None (`null`) | Everyone with access to the template uses one workspace. |
| User (`"user"`) | Each user gets their own workspace, reused across their chats. |
| Chat (`"chat"`) | Each saved chat gets a workspace shared by people allowed to continue that chat. |
| Model (`"model"`) | Each model or preset gets a workspace shared across users and chats that have access. |

**New templates default to None. Choose User for separate personal files.** Chat scope needs a saved chat; model scope needs a selected model. Missing information causes an error instead of opening a different workspace.

Model scope follows the model or preset selected in WebUI. Two presets using the same provider model get separate workspaces. Changing a preset's provider model keeps its workspace.

### Terminal management

- **Create, configure, and share terminals from WebUI.** Each template has its own Use and Manage permissions. A new personal template is private until shared. Users also need access to its connection.
- **Shared chats use their selected terminal.** Collaborators can use it while the chat owner retains access to the template. Sharing a read-only snapshot does not allow terminal use.
- **Stopped workspaces remain listed.** The environment list includes stopped terminals whose files remain. Checking status or viewing the list does not start them.
- **Renaming keeps the same files.** A template's ID stays the same when its name changes. Image and other instance setting changes take effect when the terminal next starts. Delete its existing environments before changing scope or storage mode.

### Restart, reset, and delete

| Action | What it does |
| --- | --- |
| Restart | Stops running work and keeps files. The next use starts the terminal with the current template settings. |
| Reset | Stops running work and clears that workspace's files. |
| Delete this environment | Removes that workspace and its saved files. Selecting the same template for the same user, chat, or model creates an empty workspace. |
| Delete terminal | Deletes the template and all its workspaces and files. |

Restart and reset do nothing for a workspace that has never been created. Deleting an already removed environment is safe to repeat. If deletion fails partway through, Terminals keeps records of what remains so you can retry.

Terminals tracks workspaces whether or not they have a reset schedule. Scheduled resets, image restrictions, resource limits, network profiles, security settings, idle cleanup, and maximum lifetime remain available.

### Other changes

- **The built-in admin UI has Templates and Environments.** It includes create, configure, restart, reset, and delete actions. Name and scope have their own fields; advanced settings and reset schedules use JSON editors. The old bulk rollout and user-data screens are removed.
- **Automations use the same scope choices.** User uses the automation owner; Model uses its selected model; Chat uses the saved chat created for that run; None uses the template's shared workspace. Runs that create new chats get separate Chat workspaces. Channel automations without a saved chat cannot use Chat scope.
- **New installations start without a default template.** Create one before running commands. An old default policy can be migrated as an ordinary template that you can edit or delete.
- **Raw Open Terminal connections and Tether devices keep their existing connection paths.** The template changes apply to terminals managed by this service.
- **Docker hosts now pull a newer `:latest` image.** Before, Docker kept whichever image it first downloaded, so a host could stay on an old Open Terminal indefinitely. Terminals now checks the registry before starting a workspace from an image tagged `:latest` or untagged, at most once an hour per image, and uses the local copy if the registry can't be reached. Pinned tags are pulled only when missing. Set `TERMINALS_IMAGE_PULL_POLICY` to `Always`, `IfNotPresent`, or `Never` to choose one behavior for every image; it also applies to Kubernetes pods and operator-managed Terminals. Running workspaces keep their image until they restart.
- **Kubernetes workspaces start again.** Terminal and reset pods were created with an invalid field and failed on every start.

### Breaking changes for administrators and integrations

**Policies are now templates.** Replace `per_chat`, `per_automation`, and connection-level allocation settings with the template's `data.scope`. Templates are the items users select and share; there is no separate named-workspace resource or resolve request.

**WebUI connects using the Terminals server key.** It checks permissions and sends the verified user, chat, and model IDs. Terminals no longer asks WebUI to validate user tokens. Remove `TERMINALS_OPEN_WEBUI_URL` and `TERMINALS_TOKEN_CACHE_TTL` from your settings. WebUI user JWTs no longer authorize managed terminal requests.

**Execution uses headers instead of policy-prefixed URLs.** Stop using `/p/{policy_id}/...`. HTTP and WebSocket requests use `X-Terminal-Template-Id` and `X-User-Id`, plus `X-Terminal-Chat-Id` or `X-Terminal-Model-Id` when required. `/openapi.json` uses the same server key and verified user/chat/model context as execution, and uses the selected instance. Caller-selected `X-Terminal-Context-Id` values are rejected. Scope is read from the current template on each request; clients do not send or store a scope override.

**Saved selections use `template:<connection-id>:<encoded-template-id>`.** Update saved selections and permissions together. Reusing an old display name does not reconnect them.

The API changes are:

| Previous API | Replacement or change |
| --- | --- |
| `/api/v1/policies` | `/api/v1/terminals/templates`; edit a template with `PATCH`, not `PUT` |
| `/api/v1/policies/{id}/lifecycle` | `/api/v1/terminals/templates/{id}/lifecycle`; still uses `GET` and `PUT` |
| `/api/v1/status` | `/api/v1/terminals/status`; `policy_count` becomes `template_count` |
| `/api/v1/terminals` | Lists both running and stopped environments, with `id`, `template_id`, and `context_id` |
| `/api/v1/terminals/stop` and `/refresh` | Use the template's `/restart` or `/reset` action for one environment |
| `/api/v1/terminals/data` and `/cleanup` | Use the environment list and delete individual environments |

There is no replacement for bulk idle-only refresh or whole-user cleanup. The status response also drops active terminal and WebSocket counts, and the global idle and maximum-lifetime settings. Update clients that read these fields.

Environment APIs sit below `/api/v1/terminals/templates/{id}`:

| Request | Purpose |
| --- | --- |
| `GET /instances` | List the template's tracked instances, including stopped ones, without creating any |
| `GET /instance` | Check a workspace without starting it |
| `POST /restart` | Stop it and keep files |
| `POST /reset` | Stop it and clear files |
| `DELETE /instance` | Delete the workspace and its record |
| `DELETE` on the template itself | Delete the template and all its workspaces |

Supply the user, chat, or model IDs needed by the template. Terminals uses the stored scope to choose the workspace; callers do not choose a instance address.

**Container names and file locations change.** They now depend on the template and its scope, so chat and model workspaces no longer depend on who opened them first. Copy existing files to their new locations during migration. On Kubernetes, update the operator and CRDs too: the Terminal resource uses `spec.templateId` instead of `spec.userId`. Update custom manifests that create these resources.

**Existing databases do not upgrade at server startup.** Migrate them explicitly or use the fresh-installation route below. Fresh databases still initialize automatically. Reverting the image alone cannot undo a migration.

### Moving to 0.4.0 manually

You do not need a migration script. Set up a fresh 0.4.0 installation, recreate your templates, and copy your files. A script can help with larger installations, but it is optional.

This route uses a new Terminals database. It keeps the files and settings you transfer, but starts new activity and reset histories. Keep your old installation and backups until you have checked the new one.

1. **Make a list of what needs to move.** Record policies, reset schedules, connection settings, permissions, and saved terminal selections. For each workspace, note its old user/chat/automation, file location, and intended new template and scope. Check actual container or pod mounts to find the files, including storage from stopped terminals. Export any needed files stored only inside a container before removing it.

2. **Stop writes and back up.** Restrict WebUI access, pause automations, and stop old terminal workloads and services. Make sure an operator cannot recreate them. Back up both databases, settings, and file storage. Stopping the Terminals service alone does not stop its containers or pods. Stop WebUI while taking its database backup too.

3. **Start a separate 0.4.0 installation.** Set `TERMINALS_DATABASE_URL` to a new, empty database. Keep the old database untouched; do not mark it as migrated. For Docker, choose a new `TERMINALS_DOCKER_DATA_DIR` and matching host path if needed. For Kubernetes, use separate destination storage and preferably a separate namespace, with the matching operator and CRDs. Start the matching WebUI version with access limited to migration administrators. Connect using the Terminals server key and remove the old JWT settings.

4. **Recreate templates and access.** Use **New terminal** in WebUI or the template API. Copy the old image, environment variables, resource limits, storage, network, and security settings. Preserve server defaults and who can access each connection and template. Creating a template through the Terminals admin API does not create WebUI permissions. Record the new IDs. Keep scheduled resets disabled until the files are copied and checked.

5. **Prepare each new workspace.** Open it with the correct existing user, saved chat, or model and run a harmless command. This creates its storage and tracking record. For Chat scope, select the template on the existing chat; making a new chat changes which workspace it uses. Get the instance `id` from `GET /api/v1/terminals/templates/{id}/instance`, supplying `user_id` and the relevant `chat_id` or `model_id`. This status request alone does not create a workspace. Record its storage mount. Use **Restart** to stop the terminal while keeping files, confirm it has stopped, block reconnections, and stop the new Terminals service during copying. Do not use Reset or Delete here.

6. **Copy and check files.** On Docker, copy the old home contents into `.terminals/<instance-id>` under the new data directory. Use the matching Docker-host directory if a host path override is configured. Include hidden files and preserve ownership, permissions, and symlinks. On Kubernetes, use temporary pods to copy between the old and new PVCs, with the source mounted read-only and the PVC access rules respected. Shared PVCs use `.terminals/<instance-id>`; for dedicated PVCs, use the new terminal's recorded home mount. Copy extra persistent mounts where needed. Compare file contents or checksums and file metadata before starting the terminal again.

7. **Update saved terminal choices.** Select the new templates in chats, automations, folders, model settings, and connection defaults. Update template permissions too. Use WebUI's controls where available. If a saved choice has no editor, update that specific record in a backed-up WebUI database while WebUI is stopped. Follow your old-to-new list; do not globally replace policy IDs. A matching name does not transfer saved selections or permissions.

8. **Check before letting users back in.** Test with the affected users. Confirm files are present, personal files stay private, shared chats use the same workspace, and models and automations use the right templates. Check that stopped workspaces remain listed and that saved choices no longer point to old workspaces. Restore reset schedules and resume automations after these checks. Keep the old files, databases, and your migration notes for rollback.

When choosing scopes, keep these cases in mind:

- **Old personal workspaces:** choose User, even if the old settings did not explicitly say “per user.” None would share the workspace with everyone who can use that template.
- **Different files for people in the same chat:** use the chat owner's files for the new shared chat workspace. Keep everyone else's files in separate private templates. Do not merge their folders.
- **Old automation workspaces:** create ordinary templates for them and select those templates in the automations.
- **Different connection rules or shared folders:** use separate templates where the old rules differ. If old workspaces used one folder, give each new workspace its own copy before allowing separate reset or deletion.

If you want to start with empty workspaces, skip copying old files and settings. Keep a backup, create the new templates and permissions, and still update WebUI's saved terminal choices.

### Database migration command

`terminals db upgrade` applies schema migrations to fresh or empty databases. It does not copy files or rewrite WebUI data. Populated pre-0.4 databases are rejected before migration 004 changes their tables. Use the manual migration above, retaining the old database and storage.

### Going back to the old version

Stop the new services and workloads. Restore the pre-migration WebUI database and settings, then run the old Terminals and WebUI versions against the retained old databases and storage. Restore the old operator and CRDs where needed.

Files changed after users start using 0.4.0 are not in the old backups; recover those changes separately if needed. Keep access restricted until migration checks pass so rollback is simpler.

## [0.3.3] - 2026-11-07

### Added

- Added an optional maximum terminal lifetime in minutes, configurable for the whole service or through **Max lifetime** in each policy's editor. Once the limit is reached, the terminal stops even while in use, ending running commands. Files in persistent storage are kept, and the next use starts a fresh terminal with those files. The limit is disabled by default.

### Fixed

- Fixed unused terminal tabs preventing idle cleanup by repeatedly sending background connection checks. These checks no longer reset the idle timer; typing and command output still count as activity.

## [0.3.2] - 2026-11-07

### Fixed

- Fixed opening or refreshing System info starting a terminal or preventing idle cleanup. It now inspects only an already-running terminal for the selected user, policy, and chat, and reports when that terminal is not running.
- Fixed idle cleanup stopping terminals while users were typing, receiving command output, or using live browser and desktop views. Traffic in either direction now counts as activity.
- Fixed live connections failing to recognize the user supplied by Open WebUI. Terminal and agent connections now also retain their chat identity and resume position when reconnecting.
- Fixed live connections closing when a single incoming message exceeded 1 MiB. They now accept messages up to 20 MiB, including larger desktop frames.
- Fixed connections remaining open after the browser disconnected or the Open Terminal stream ended. Both sides now close when either side finishes.

## [0.3.1] - 2026-11-05

### Fixed

- Fixed Open WebUI failing to load terminal tools with the Terminals admin API key when Session authentication was enabled. This now works for both default connections and named policies. Running commands and accessing files still require the user's valid session.

## [0.3.0] - 2026-11-05

### Added

- Added optional network profiles to terminal policies on Docker and both Kubernetes deployment modes. Different policies can now select administrator-configured networks and access rules, such as access to internal services, internet-only access, or no external access.
- Added a **Network profile** field to the admin policy editor. Leaving it blank keeps the existing networking defaults. Unknown profiles and profiles missing settings for the selected deployment type are rejected instead of silently using the default network.
- Added setup instructions for defining profiles with `TERMINALS_NETWORK_PROFILES` and applying them to existing terminals. Administrators must create the networks and access rules themselves; naming a profile `restricted` does not block traffic on its own. Docker host networking is not supported by profiles.

### Fixed

- Fixed Docker reconnections using the service's default network setting instead of the terminal's actual network. Terminals on policy-specific networks can now be reached correctly when Terminals restarts or reconnects to an existing container.

## [0.2.9] - 2026-11-05

### Added

- Added a **User data** list to the admin UI showing users with saved terminal files, including users whose terminals have already stopped.
- Added **Delete user data** to remove a user's saved terminal files across policies, chats, and automation runs. After confirmation, it stops their terminals and refuses to delete files if terminal resources remain. Cleanup is also available through the admin API. Deleting an Open WebUI account does not trigger it automatically; shared Kubernetes storage still requires separate cleanup.

### Changed

- Made the admin UI more compact with shorter table rows, less vertical space between sections and form fields, softer borders, and smaller gray delete actions.
- Rewrote the README around three setup steps: start Terminals, open the admin UI, and connect Open WebUI. The Docker commands now generate an API key and connect services through one network. Added troubleshooting and clearer instructions for saved files, deletion, and Kubernetes setup.

### Fixed

- Fixed both Kubernetes deployment modes ignoring custom terminal health-check settings. Policies can now give slow terminals more time to respond before they are marked unavailable or restarted. Existing terminals must be refreshed to use the updated settings.

## [0.2.8] - 2026-11-05

### Added

- Added a configurable startup wait for Kubernetes operator deployments so slower terminal starts can finish before users see a timeout. Set `TERMINALS_PROVISION_TIMEOUT_SECONDS=300` to wait up to five minutes. The default remains 120 seconds.

### Fixed

- Fixed Docker terminal creation failing when the selected image had not been downloaded to the host. Terminals now downloads missing images automatically before starting the terminal.
- Fixed named policies ignoring the configured Kubernetes image and using the general default instead, which could launch `latest` instead of a pinned version. Both Kubernetes deployment modes now respect the Kubernetes image setting unless the policy selects its own image.
- Fixed Kubernetes operator deployments making users wait for a timeout when the cluster had already rejected a terminal start because of exhausted capacity or permissions. Requests now report the reason promptly, and the operator stops retrying that rejected start instead of creating an unwanted terminal later. A new user request can retry.

## [0.2.7] - 2026-11-05

### Fixed

- Fixed Open Terminal browser and desktop live views, live updates, and streaming command output failing to connect through Terminals, including connections using named policies.
- Fixed manual and scheduled file resets being rejected by Kubernetes clusters with strict security or resource rules. Resets now use the terminal's configured image, security settings, and placement rules in both Kubernetes deployment modes.
- Fixed Kubernetes operator deployments leaving users unable to reconnect after a terminal pod was deleted. Terminals now recreates the missing terminal on the next access while keeping files in persistent storage, even if the operator missed the deletion.
- Fixed errors appearing after requests finished because the connection to Open Terminal was not closed correctly. Completed responses now release that connection without the cleanup error.

## [0.2.6] - 2026-11-05

### Fixed

- Fixed Kubernetes operator deployments stopping terminals after 30 minutes even when idle cleanup was disabled. Setting the idle timeout to `0` now keeps idle terminals running, preventing the loss of files stored only inside those terminals.
- Fixed the Docker image saving its database outside the mounted data folder, which could lose saved policies when the Terminals container was replaced. The database now lives in `/app/data/terminals.db`.
- Fixed Docker terminal files being stored outside the intended workspace folder when Terminals runs in a container. The optional `TERMINALS_DOCKER_HOST_DATA_DIR` setting lets terminal creation and file resets use the same files when the host and container see that folder at different paths. The Docker quick start now includes this setup.
- Fixed terminals on named Docker networks opening unnecessary ports on the host. Newly created terminals on those networks are reached through the Docker network without publishing a host port.

## [0.2.5] - 2026-10-16

### Fixed

- Fixed terminal windows showing "[Connection closed]" immediately after opening in saved Open WebUI chats when using Open Terminal 0.12.4 or newer.
- Corrected the Docker quick start command to use the published Terminals image and set the host address needed to reach terminal containers.

## [0.2.4] - 2026-09-12

### Fixed

- Blocked requests made through an Open WebUI user's terminal connection from viewing the full terminal list, changing policies or reset schedules, and stopping or resetting other users' terminals.

## [0.2.3] - 2026-08-31

### Fixed
- Fixed PostgreSQL policy create and update requests so they no longer fail when saving policy timestamps.
- Fixed the app version shown by the running service so it matches the package version.

## [0.2.2] - 2026-08-22

### Fixed
- Fixed PostgreSQL support so fresh installs include the needed database drivers.
- Fixed `postgres://` and `postgresql://` database URLs so they work without operators having to know the exact driver name.
- Fixed startup and migration failures when PostgreSQL passwords contain URL-escaped characters like `%25`, `%7D`, `%3A`, or `%26`.

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
- Split proxy connection pools by terminal environment so heavy traffic to one terminal does not slow down unrelated terminal sessions.
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
