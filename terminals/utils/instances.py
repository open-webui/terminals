"""Cross-worker locks and cleanup for deterministic terminal environments."""
import asyncio
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from terminals.db.session import engine

@asynccontextmanager
async def instance_lock(key_name, *, shared=False):
    # Do not hold a SQLite write transaction while the backend persists activity.
    # The lock file is shared by all workers using this database and is never unlinked.
    if engine.dialect.name == "sqlite":
        import os

        directory = Path(engine.url.database).resolve().parent / ".terminal-locks"
        directory.mkdir(parents=True, exist_ok=True)
        lock = open(directory / hashlib.sha256(key_name.encode()).hexdigest(), "a+b")
        if os.name == "nt":
            import msvcrt

            if lock.tell() == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            acquire = lambda: msvcrt.locking(lock.fileno(), (msvcrt.LK_NBRLCK if shared else msvcrt.LK_NBLCK), 1)
        else:
            import fcntl

            acquire = lambda: fcntl.flock(lock, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        try:
            deadline = asyncio.get_running_loop().time() + 5
            while True:
                try:
                    acquire()
                    break
                except (BlockingIOError, OSError):
                    if asyncio.get_running_loop().time() >= deadline:
                        raise HTTPException(409, "Environment is busy; retry shortly")
                    await asyncio.sleep(0.05)
            yield
        finally:
            lock.close()
    else:
        key = int.from_bytes(
            hashlib.sha256(key_name.encode()).digest()[:8], "big", signed=True
        )
        async with engine.connect() as connection:
            async with connection.begin():
                await connection.execute(text("SET LOCAL lock_timeout = '5s'"))
                try:
                    await connection.execute(
                        text("SELECT pg_advisory_xact_lock_shared(:key)" if shared else "SELECT pg_advisory_xact_lock(:key)"), {"key": key}
                    )
                except DBAPIError as exc:
                    if getattr(exc.orig, "sqlstate", None) == "55P03":
                        raise HTTPException(
                            409, "Environment is busy; retry shortly"
                        ) from exc
                    raise
                yield


async def stop_instance(backend, template_id, context_id):
    """Stop by deterministic identity, including resources unknown to this worker."""
    from terminals.routers.proxy import close_instance_sessions
    from terminals.utils.context import get_terminal_id
    await close_instance_sessions(get_terminal_id(template_id, context_id))
    from terminals.config import settings

    if settings.backend == "docker":
        from aiodocker.exceptions import DockerError

        docker = await backend._get_docker()
        name = backend._container_name(template_id, context_id)
        try:
            container = await docker.containers.get(name)
            await container.delete(force=True)
        except DockerError as exc:
            if exc.status != 404:
                raise
        try:
            await docker.containers.get(name)
        except DockerError as exc:
            if exc.status != 404:
                raise
        else:
            raise HTTPException(409, "Environment is still stopping")
    else:
        from kubernetes_asyncio import client
        from terminals.backends.kubernetes import _sanitize_name

        api = await backend._ensure_client()
        core = client.CoreV1Api(api)
        name = _sanitize_name(template_id, context_id)
        ns = settings.kubernetes_namespace
        if settings.backend == "kubernetes-operator":
            await backend._delete_terminal_cr(
                template_id, context_id
            )
            # A deleted CR can still have live dependent pods during garbage collection.
            selector = f"openwebui.com/terminal={name}"
        else:
            selector = None
            for method, resource in (
                (core.delete_namespaced_pod, name),
                (core.delete_namespaced_service, name),
                (core.delete_namespaced_secret, name + "-apikey"),
            ):
                try:
                    await method(resource, ns)
                except client.exceptions.ApiException as exc:
                    if exc.status != 404:
                        raise
        async with asyncio.timeout(120):
            while True:
                if selector:
                    pods = (
                        await core.list_namespaced_pod(ns, label_selector=selector)
                    ).items
                else:
                    try:
                        pods = [await core.read_namespaced_pod(name, ns)]
                    except client.exceptions.ApiException as exc:
                        if exc.status != 404:
                            raise
                        pods = []
                if not pods:
                    break
                await asyncio.sleep(0.2)
    backend.forget(backend._key(template_id, context_id))


async def remove_home(backend, row, spec):
    from terminals.config import settings

    if settings.backend == "docker":
        import shutil

        home = backend._home_dir(row.template_id, row.context_id)
        if home.exists():
            await asyncio.to_thread(shutil.rmtree, home)
    elif settings.backend == "kubernetes" and spec.get(
        "storage_mode", settings.kubernetes_storage_mode
    ) in ("shared", "shared-rwo"):
        await backend.reset(row.template_id, row.context_id, spec)
    else:
        from kubernetes_asyncio import client
        from terminals.backends.kubernetes import _sanitize_name

        core = client.CoreV1Api(await backend._ensure_client())
        name = _sanitize_name(row.template_id, row.context_id)
        if settings.backend == "kubernetes-operator":
            name += "-pvc"
        try:
            await core.delete_namespaced_persistent_volume_claim(
                name, settings.kubernetes_namespace
            )
        except client.exceptions.ApiException as exc:
            if exc.status != 404:
                raise

        async with asyncio.timeout(120):
            while True:
                try:
                    await core.read_namespaced_persistent_volume_claim(
                        name, settings.kubernetes_namespace
                    )
                except client.exceptions.ApiException as exc:
                    if exc.status == 404:
                        break
                    raise
                await asyncio.sleep(0.2)


async def describe_instance(backend, row):
    status = await backend.status(f"terminal-{row.id}")
    return {
        "id": row.id,
        "template_id": row.template_id,
        "context_id": row.context_id,
        "status": "stopped" if status == "missing" else status,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "last_active_at": (row.data or {}).get("activity", {}).get("last_seen_at"),
    }


async def change_instance(backend, template_id, context_id, action):
    """Stop, restart, reset, or delete one environment.

    Restart stops the environment and starts it again on the current template, so a
    new image or setup script takes effect right away. Stop leaves it stopped until
    it is next used or started.
    """
    from terminals.db.session import async_session
    from terminals.models.template import TemplateLifecycleState
    from terminals.utils.context import get_terminal_id
    from terminals.utils.template_specs import resolve_template_spec
    key = get_terminal_id(template_id, context_id)
    async with instance_lock("template:" + template_id, shared=True), instance_lock(key):
        spec = await resolve_template_spec(template_id)
        async with async_session() as session:
            row = await session.get(TemplateLifecycleState, key)
            if row is None:
                if action == "delete":
                    return
                raise HTTPException(404, "Environment has not been created")
            await stop_instance(backend, row.template_id, row.context_id)
            if action == "reset":
                await backend.reset(template_id, context_id, spec)
            elif action == "delete":
                await remove_home(backend, row, spec)
                await session.delete(row)
                await session.commit()
    if action == "restart":
        # Outside the locks: starting takes the same environment lock.
        await backend.ensure_terminal(template_id=template_id, context_id=context_id)
