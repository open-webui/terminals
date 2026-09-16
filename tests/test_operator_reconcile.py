"""Tests for the operator's pod reconciliation.

``operator/handler.py`` is loaded straight from its path — it is a kopf entry
point, not an importable package, and ``operator`` is a stdlib module name.
``kopf`` and ``kubernetes`` are stubbed so the handler logic can be exercised
without a cluster or those dependencies installed.
"""

import asyncio
import base64
import importlib.util
import os
import pathlib
import sys
import types
import unittest
from types import SimpleNamespace


# ---------------------------------------------------------------------------
# Stubs for kopf / kubernetes
# ---------------------------------------------------------------------------


class ApiException(Exception):
    def __init__(self, status: int = 500, reason: str = "") -> None:
        super().__init__(f"({status}) {reason}")
        self.status = status


def _identity_decorator(*_args, **_kwargs):
    def wrap(fn):
        return fn

    return wrap


def _install_stubs() -> None:
    kopf = types.ModuleType("kopf")
    kopf.on = SimpleNamespace(
        startup=_identity_decorator,
        create=_identity_decorator,
        delete=_identity_decorator,
        event=_identity_decorator,
        resume=_identity_decorator,
    )
    kopf.timer = _identity_decorator
    kopf.OperatorSettings = object
    sys.modules.setdefault("kopf", kopf)

    k8s_client = types.ModuleType("kubernetes.client")
    k8s_client.exceptions = SimpleNamespace(ApiException=ApiException)
    k8s_client.CoreV1Api = lambda *a, **kw: None
    k8s_client.CustomObjectsApi = lambda *a, **kw: None

    kubernetes = types.ModuleType("kubernetes")
    kubernetes.client = k8s_client
    kubernetes.config = SimpleNamespace(
        load_incluster_config=lambda: None,
        load_kube_config=lambda: None,
        ConfigException=Exception,
    )
    sys.modules.setdefault("kubernetes", kubernetes)
    sys.modules.setdefault("kubernetes.client", k8s_client)


def _load_handler():
    _install_stubs()
    path = pathlib.Path(__file__).resolve().parent.parent / "operator" / "handler.py"
    spec = importlib.util.spec_from_file_location("terminal_operator_handler", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


handler = _load_handler()


# ---------------------------------------------------------------------------
# Fake Kubernetes API
# ---------------------------------------------------------------------------


class FakeCoreV1:
    def __init__(self) -> None:
        self.pods: dict[str, SimpleNamespace] = {}
        self.services: set[str] = set()
        self.secrets: dict[str, dict] = {}
        self.pvcs: set[str] = set()
        self.created_pods: list[dict] = []
        self.deleted_pods: list[str] = []

    # -- pods
    def add_pod(self, name: str, phase: str = "Running", deleting: bool = False) -> None:
        self.pods[name] = SimpleNamespace(
            metadata=SimpleNamespace(deletion_timestamp="now" if deleting else None),
            status=SimpleNamespace(phase=phase),
        )

    def read_namespaced_pod(self, name, namespace):
        if name not in self.pods:
            raise ApiException(404, "pod not found")
        return self.pods[name]

    def create_namespaced_pod(self, namespace=None, body=None):
        name = body["metadata"]["name"]
        if name in self.pods:
            raise ApiException(409, "already exists")
        self.created_pods.append(body)
        self.add_pod(name, phase="Pending")

    def delete_namespaced_pod(self, name=None, namespace=None):
        if name not in self.pods:
            raise ApiException(404, "pod not found")
        self.deleted_pods.append(name)
        del self.pods[name]

    # -- services
    def read_namespaced_service(self, name, namespace):
        if name not in self.services:
            raise ApiException(404, "service not found")
        return SimpleNamespace(metadata=SimpleNamespace(name=name))

    def create_namespaced_service(self, namespace=None, body=None):
        self.services.add(body["metadata"]["name"])

    # -- secrets
    def add_secret(self, name: str, api_key: str) -> None:
        self.secrets[name] = {
            "api-key": base64.b64encode(api_key.encode()).decode()
        }

    def read_namespaced_secret(self, name, namespace):
        if name not in self.secrets:
            raise ApiException(404, "secret not found")
        return SimpleNamespace(data=self.secrets[name])

    def create_namespaced_secret(self, namespace=None, body=None):
        name = body["metadata"]["name"]
        if name in self.secrets:
            raise ApiException(409, "already exists")
        self.secrets[name] = body["data"]

    def patch_namespaced_secret(self, name, namespace, body):
        self.secrets.setdefault(name, {}).update(body["data"])

    # -- pvcs
    def create_namespaced_persistent_volume_claim(self, namespace=None, body=None):
        self.pvcs.add(body["metadata"]["name"])


class FakeCustomObjects:
    def __init__(self, terminals: dict[str, dict]) -> None:
        self.terminals = terminals
        self.status_patches: list[tuple[str, dict]] = []

    def get_namespaced_custom_object(self, group=None, version=None, namespace=None,
                                     plural=None, name=None):
        if name not in self.terminals:
            raise ApiException(404, "terminal not found")
        return self.terminals[name]

    def patch_namespaced_custom_object_status(self, group=None, version=None,
                                              namespace=None, plural=None, name=None,
                                              body=None, _content_type=None):
        if _content_type != "application/merge-patch+json":
            raise ApiException(415, f"unsupported content type {_content_type!r}")
        if name not in self.terminals:
            raise ApiException(404, "terminal not found")
        status = self.terminals[name].setdefault("status", {})
        status.update(body["status"])
        self.status_patches.append((name, body["status"]))


NAME = "terminal-abc"
NAMESPACE = "terminals"
POD = f"{NAME}-pod"
SECRET = f"{NAME}-apikey"


def _terminal(phase: str = "Running", **status) -> dict:
    return {
        "apiVersion": f"{handler.GROUP}/{handler.VERSION}",
        "kind": "Terminal",
        "metadata": {"name": NAME, "namespace": NAMESPACE, "uid": "uid-1"},
        "spec": {"userId": "u1", "persistence": {"enabled": False}},
        "status": {
            "phase": phase,
            "podName": POD,
            "serviceName": f"{NAME}-svc",
            "serviceUrl": f"http://{NAME}-svc.{NAMESPACE}.svc:8000",
            "apiKeySecret": SECRET,
            **status,
        },
    }


class ReconcileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.core = FakeCoreV1()
        self.core.services.add(f"{NAME}-svc")
        self.core.add_secret(SECRET, "sk-existing")
        self.terminals: dict[str, dict] = {}
        self.custom = FakeCustomObjects(self.terminals)
        patcher = SimpleNamespace(
            CoreV1Api=lambda *a, **kw: self.core,
            CustomObjectsApi=lambda *a, **kw: self.custom,
            exceptions=SimpleNamespace(ApiException=ApiException),
        )
        self._orig_k8s = handler.k8s
        handler.k8s = patcher
        self.addCleanup(setattr, handler, "k8s", self._orig_k8s)

    def _reconcile(self, terminal: dict, trigger: str = "timer") -> None:
        self.terminals[terminal["metadata"]["name"]] = terminal
        handler._reconcile(
            terminal,
            terminal["spec"],
            terminal.get("status"),
            terminal["metadata"],
            terminal["metadata"]["name"],
            NAMESPACE,
            trigger,
        )

    # -- the reported bug: pod deleted, nothing re-created it -----------------

    def test_recreates_a_missing_pod(self) -> None:
        terminal = _terminal(phase="Running")
        self._reconcile(terminal)

        self.assertEqual(len(self.core.created_pods), 1)
        self.assertEqual(self.core.created_pods[0]["metadata"]["name"], POD)
        self.assertEqual(terminal["status"]["phase"], "Pending")

    def test_recreated_pod_keeps_the_existing_api_key(self) -> None:
        self._reconcile(_terminal(phase="Running"))

        env = self.core.created_pods[0]["spec"]["containers"][0]["env"]
        keys = [e["value"] for e in env if e["name"] == "OPEN_TERMINAL_API_KEY"]
        self.assertEqual(keys, ["sk-existing"])

    def test_running_status_is_cleared_so_the_proxy_stops_routing(self) -> None:
        terminal = _terminal(phase="Running")
        self._reconcile(terminal)

        ready = [
            c for c in terminal["status"]["conditions"] if c["type"] == "Ready"
        ]
        self.assertEqual(ready[0]["status"], "False")
        self.assertEqual(ready[0]["reason"], "PodRecreated")

    def test_live_pod_is_left_alone(self) -> None:
        self.core.add_pod(POD, phase="Running")
        self._reconcile(_terminal(phase="Running"))

        self.assertEqual(self.core.created_pods, [])
        self.assertEqual(self.custom.status_patches, [])

    def test_failed_pod_is_replaced(self) -> None:
        self.core.add_pod(POD, phase="Failed")
        self._reconcile(_terminal(phase="Error"))

        self.assertEqual(self.core.deleted_pods, [POD])

    def test_terminating_pod_drops_out_of_running(self) -> None:
        self.core.add_pod(POD, phase="Running", deleting=True)
        terminal = _terminal(phase="Running")
        self._reconcile(terminal)

        self.assertEqual(self.core.created_pods, [])
        self.assertEqual(terminal["status"]["phase"], "Pending")

    def test_missing_service_is_recreated(self) -> None:
        self.core.services.clear()
        self.core.add_pod(POD, phase="Running")
        self._reconcile(_terminal(phase="Running"))

        self.assertIn(f"{NAME}-svc", self.core.services)

    # -- deliberate teardowns must not be undone -----------------------------

    def test_idle_terminals_are_not_repaired(self) -> None:
        self._reconcile(_terminal(phase="Idle"))

        self.assertEqual(self.core.created_pods, [])

    def test_idle_race_is_caught_by_the_live_phase_guard(self) -> None:
        # Cached status still says Running, but the CR is already Idle.
        stale = _terminal(phase="Running")
        live = _terminal(phase="Idle")
        self.terminals[NAME] = live
        handler._reconcile(
            stale, stale["spec"], stale["status"], stale["metadata"],
            NAME, NAMESPACE, "timer",
        )

        self.assertEqual(self.core.created_pods, [])

    def test_terminals_being_deleted_are_not_repaired(self) -> None:
        terminal = _terminal(phase="Running")
        terminal["metadata"]["deletionTimestamp"] = "now"
        self._reconcile(terminal)

        self.assertEqual(self.core.created_pods, [])


class PodEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self.core = FakeCoreV1()
        self.core.services.add(f"{NAME}-svc")
        self.core.add_secret(SECRET, "sk-existing")
        self.terminals: dict[str, dict] = {}
        self.custom = FakeCustomObjects(self.terminals)
        handler.k8s = SimpleNamespace(
            CoreV1Api=lambda *a, **kw: self.core,
            CustomObjectsApi=lambda *a, **kw: self.custom,
            exceptions=SimpleNamespace(ApiException=ApiException),
        )

    def _pod_body(self, phase: str = "Running", ready: bool = True) -> dict:
        return {
            "metadata": {
                "name": POD,
                "namespace": NAMESPACE,
                "labels": {
                    "app.kubernetes.io/managed-by": "terminals",
                    "openwebui.com/terminal": NAME,
                },
            },
            "status": {"phase": phase, "containerStatuses": [{"ready": ready}]},
        }

    def _event(self, event_type: str, body: dict) -> None:
        asyncio.run(handler.on_pod_event(event={"type": event_type}, body=body))

    def test_deleted_pod_flips_status_and_recreates(self) -> None:
        terminal = _terminal(phase="Running")
        self.terminals[NAME] = terminal

        self._event("DELETED", self._pod_body())

        self.assertEqual(terminal["status"]["phase"], "Pending")
        self.assertEqual(len(self.core.created_pods), 1)

    def test_deleted_pod_of_an_idle_terminal_is_left_down(self) -> None:
        self.terminals[NAME] = _terminal(phase="Idle")

        self._event("DELETED", self._pod_body())

        self.assertEqual(self.core.created_pods, [])

    def test_ready_pod_marks_the_terminal_running(self) -> None:
        terminal = _terminal(phase="Pending")
        self.terminals[NAME] = terminal

        self._event("MODIFIED", self._pod_body(phase="Running", ready=True))

        self.assertEqual(terminal["status"]["phase"], "Running")

    def test_terminating_pod_is_not_reported_ready(self) -> None:
        terminal = _terminal(phase="Running")
        self.terminals[NAME] = terminal
        body = self._pod_body(phase="Running", ready=True)
        body["metadata"]["deletionTimestamp"] = "now"

        self._event("MODIFIED", body)

        self.assertEqual(terminal["status"]["phase"], "Pending")


class SchedulingConfigTests(unittest.TestCase):
    """Scheduling env vars are parsed once at startup, not per pod build.

    A malformed value used to raise out of ``_build_pod_manifest``, which the
    reconcile timer calls every sweep — so a typo meant no pod was ever
    created and the traceback repeated forever.
    """

    def setUp(self) -> None:
        self._saved = (handler.NODE_SELECTOR, handler.TOLERATIONS)
        self.addCleanup(self._restore)
        for var in ("TERMINALS_KUBERNETES_NODE_SELECTOR", "TERMINALS_KUBERNETES_TOLERATIONS"):
            os.environ.pop(var, None)
            self.addCleanup(os.environ.pop, var, None)

    def _restore(self) -> None:
        handler.NODE_SELECTOR, handler.TOLERATIONS = self._saved

    def test_trailing_junk_names_the_offending_variable(self) -> None:
        os.environ["TERMINALS_KUBERNETES_TOLERATIONS"] = '[{"key": "a"}]\n[{"key": "b"}]'

        with self.assertRaises(ValueError) as caught:
            handler._load_scheduling_config()

        self.assertIn("TERMINALS_KUBERNETES_TOLERATIONS", str(caught.exception))
        self.assertIn("not valid JSON", str(caught.exception))

    def test_malformed_node_selector_names_the_offending_variable(self) -> None:
        os.environ["TERMINALS_KUBERNETES_NODE_SELECTOR"] = '{"a": "b"} {"c": "d"}'

        with self.assertRaises(ValueError) as caught:
            handler._load_scheduling_config()

        self.assertIn("TERMINALS_KUBERNETES_NODE_SELECTOR", str(caught.exception))

    def test_valid_config_is_cached_and_applied_to_pods(self) -> None:
        os.environ["TERMINALS_KUBERNETES_TOLERATIONS"] = '[{"key": "gpu", "operator": "Exists"}]'
        os.environ["TERMINALS_KUBERNETES_NODE_SELECTOR"] = "disk=ssd"

        handler._load_scheduling_config()

        self.assertEqual(handler.TOLERATIONS, [{"key": "gpu", "operator": "Exists"}])
        self.assertEqual(handler.NODE_SELECTOR, {"disk": "ssd"})

        pod = handler._build_pod_manifest(
            NAME, NAMESPACE, {}, "key", handler._owner_ref(_terminal()), None, user_id="u1"
        )
        self.assertEqual(pod["spec"]["tolerations"], [{"key": "gpu", "operator": "Exists"}])
        self.assertEqual(pod["spec"]["nodeSelector"], {"disk": "ssd"})

    def test_pod_build_does_not_reread_the_environment(self) -> None:
        handler._load_scheduling_config()
        # A value that never went through startup validation must not reach a pod.
        os.environ["TERMINALS_KUBERNETES_TOLERATIONS"] = "{not json"

        pod = handler._build_pod_manifest(
            NAME, NAMESPACE, {}, "key", handler._owner_ref(_terminal()), None, user_id="u1"
        )

        self.assertNotIn("tolerations", pod["spec"])


if __name__ == "__main__":
    unittest.main()
