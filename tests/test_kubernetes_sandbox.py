"""Unit tests for the Agent Sandbox backend.

Covers the pure manifest/status builders and the two robustness fixes:
  #3  transient API errors during the readiness wait are retried, not fatal
  #4  reconcile() re-adopts Running sandboxes after a restart so they get reaped

All tests are cluster-free: the Kubernetes API is faked at the CustomObjectsApi
boundary (`_custom`), so the real `_get_sandbox` / `_wait_until_ready` /
`reconcile` code paths run.
"""
import hashlib

import pytest
from kubernetes_asyncio.client.exceptions import ApiException

import terminals.backends.kubernetes_sandbox as ks
from terminals.backends.kubernetes_sandbox import (
    _ANN_POLICY_ID,
    _ANN_USER_ID,
    KubernetesSandboxBackend,
    _labels,
    _policy_slug,
    _sandbox_name,
    _user_hash,
)

# A shared instance for calling the *pure* builder/parsing methods (no client).
B = KubernetesSandboxBackend()


# ---------------------------------------------------------------------------
# Test fakes / helpers
# ---------------------------------------------------------------------------
class FakeCustom:
    """Stand-in for CustomObjectsApi.

    ``get_seq`` is a list consumed one-per-call, clamping at the last element;
    entries that are Exceptions are raised, dicts are returned.
    """

    def __init__(self, get_seq=None, list_result=None, list_exc=None, create_exc=None):
        self._get_seq = list(get_seq or [])
        self.get_calls = 0
        self._list_result = list_result if list_result is not None else {"items": []}
        self._list_exc = list_exc
        self.list_calls = 0
        self._create_exc = create_exc
        self.created = []
        self.patched = []
        self.deleted = []

    async def get_namespaced_custom_object(self, **kw):
        self.get_calls += 1
        item = self._get_seq[min(self.get_calls - 1, len(self._get_seq) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    async def list_namespaced_custom_object(self, **kw):
        self.list_calls += 1
        if self._list_exc:
            raise self._list_exc
        return self._list_result

    async def create_namespaced_custom_object(self, body=None, **kw):
        self.created.append(body)
        if self._create_exc:
            raise self._create_exc
        return body

    async def patch_namespaced_custom_object(self, name=None, body=None, **kw):
        self.patched.append((name, body))
        return body

    async def delete_namespaced_custom_object(self, name=None, **kw):
        self.deleted.append(name)
        return {}


def patch_custom(backend, fake):
    """Point a backend's ``_custom()`` at a FakeCustom."""
    async def _c():
        return fake
    backend._custom = _c


def make_sandbox(
    *,
    name=None,
    user_id="u1",
    policy_id="default",
    ready=True,
    suspended=False,
    generation=1,
    observed="__gen__",
    service_fqdn="term.ns.svc",
    pod_ips=None,
    api_key="sk-abc",
    with_status=True,
):
    """Build a realistic Sandbox object (spec via the real builder + a status)."""
    sb = B._build_sandbox(user_id, policy_id, {}, api_key)
    if name:
        sb["metadata"]["name"] = name
    sb["metadata"]["generation"] = generation
    if suspended:
        sb["spec"]["operatingMode"] = "Suspended"
    if with_status:
        status = {}
        if service_fqdn is not None:
            status["serviceFQDN"] = service_fqdn
        if pod_ips is not None:
            status["podIPs"] = pod_ips
        obs = generation if observed == "__gen__" else observed
        cond = {"type": "Ready", "status": "True" if ready else "False"}
        if obs is not None:
            cond["observedGeneration"] = obs
        status["conditions"] = [cond]
        sb["status"] = status
    return sb


@pytest.fixture
def no_sleep(monkeypatch):
    """Make the readiness poll loop spin without real delays."""
    async def _noop(_):
        return None
    monkeypatch.setattr(ks.asyncio, "sleep", _noop)


# ---------------------------------------------------------------------------
# Naming / label helpers
# ---------------------------------------------------------------------------
def test_policy_slug_dns_safe_and_truncated():
    assert _policy_slug("Foo Bar") == "foo-bar"
    assert _policy_slug("!!!") == "default"          # empty after strip -> fallback
    assert _policy_slug("A" * 40) == "a" * 20         # truncated to 20
    assert _policy_slug("--x--") == "x"               # hyphens stripped


def test_sandbox_name_default_and_policy():
    short = hashlib.sha256(b"user-42").hexdigest()[:12]
    assert _sandbox_name("user-42") == f"term-{short}"
    assert _sandbox_name("user-42", "Team A") == f"term-{short}-team-a"


def test_sandbox_name_is_deterministic_and_dns_bounded():
    a = _sandbox_name("someone@example.com", "a really long policy name here!!")
    b = _sandbox_name("someone@example.com", "a really long policy name here!!")
    assert a == b                       # stable across calls
    assert len(a) <= 63                  # within the DNS label limit
    assert all(c.islower() or c.isdigit() or c == "-" for c in a)


def test_labels_include_manager_and_optional_user():
    lbl = _labels("u1", "default")
    assert lbl["app.kubernetes.io/managed-by"] == "terminals"
    assert lbl["openwebui.com/user-id"] == _user_hash("u1")
    assert "openwebui.com/user-id" not in _labels("", "default")


def test_labels_user_id_hashed_to_label_safe_value():
    # Raw ids from request headers ('@', spaces, length) are not label-safe.
    val = _labels("someone@example.com" * 5, "default")["openwebui.com/user-id"]
    assert len(val) <= 63
    assert all(c.isalnum() or c in "-_." for c in val)


# ---------------------------------------------------------------------------
# _build_pod_template
# ---------------------------------------------------------------------------
def test_pod_template_defaults():
    pod = B._build_pod_template({}, "sk-key")["spec"]
    c = pod["containers"][0]
    assert c["name"] == "open-terminal"
    assert c["image"] == ks.settings.kubernetes_image      # default image
    env = {e["name"]: e["value"] for e in c["env"]}
    assert env["OPEN_TERMINAL_API_KEY"] == "sk-key"
    assert env["OPEN_TERMINAL_PORT"] == str(ks.settings.sandbox_port)
    assert c["resources"]["requests"] == {"cpu": "100m", "memory": "256Mi"}
    assert "limits" not in c["resources"]                  # none unless spec asks
    assert "volumeMounts" not in c                         # ephemeral by default
    assert pod["automountServiceAccountToken"] is False
    assert pod["enableServiceLinks"] is False
    assert c["readinessProbe"]["httpGet"]["path"] == "/health"


def test_pod_template_image_and_limits_from_spec():
    spec = {"image": "custom:1", "cpu_limit": "2", "memory_limit": "1Gi"}
    c = B._build_pod_template(spec, "k")["spec"]["containers"][0]
    assert c["image"] == "custom:1"
    assert c["resources"]["limits"] == {"cpu": "2", "memory": "1Gi"}


def test_pod_template_extra_env_stringified():
    c = B._build_pod_template({"env": {"FOO": 7}}, "k")["spec"]["containers"][0]
    env = {e["name"]: e["value"] for e in c["env"]}
    assert env["FOO"] == "7"


def test_pod_template_mounts_workspace_when_storage():
    c = B._build_pod_template({"storage": "1Gi"}, "k")["spec"]["containers"][0]
    assert c["volumeMounts"] == [{"name": "workspace", "mountPath": "/workspace"}]


def test_pod_template_runtime_class(monkeypatch):
    monkeypatch.setattr(ks.settings, "sandbox_runtime_class", "gvisor")
    pod = B._build_pod_template({}, "k")["spec"]
    assert pod["runtimeClassName"] == "gvisor"


def test_pod_template_carries_app_labels():
    # #11 — controller-created Pods must inherit the app labels for selectors.
    pt = B._build_pod_template({}, "k", "u1", "team-a")
    labels = pt["metadata"]["labels"]
    assert labels["app.kubernetes.io/managed-by"] == "terminals"
    assert labels["openwebui.com/user-id"] == _user_hash("u1")


def test_pod_template_requests_default_from_settings(monkeypatch):
    # #10 — deployment-wide default requests are configurable.
    monkeypatch.setattr(ks.settings, "sandbox_cpu_request", "250m")
    monkeypatch.setattr(ks.settings, "sandbox_memory_request", "512Mi")
    c = B._build_pod_template({}, "k")["spec"]["containers"][0]
    assert c["resources"]["requests"] == {"cpu": "250m", "memory": "512Mi"}


def test_pod_template_requests_overridden_by_spec():
    # #10 — per-policy spec overrides the defaults.
    spec = {"cpu_request": "500m", "memory_request": "1Gi"}
    c = B._build_pod_template(spec, "k")["spec"]["containers"][0]
    assert c["resources"]["requests"] == {"cpu": "500m", "memory": "1Gi"}


# ---------------------------------------------------------------------------
# _build_volume_claim_templates
# ---------------------------------------------------------------------------
def test_vct_empty_without_storage():
    assert B._build_volume_claim_templates({}) == []


def test_vct_from_spec():
    vct = B._build_volume_claim_templates({"storage": "5Gi", "storage_class": "fast"})
    assert vct[0]["metadata"]["name"] == "workspace"
    assert vct[0]["spec"]["resources"]["requests"]["storage"] == "5Gi"
    assert vct[0]["spec"]["accessModes"] == ["ReadWriteOnce"]
    assert vct[0]["spec"]["storageClassName"] == "fast"


def test_vct_storage_class_falls_back_to_settings(monkeypatch):
    monkeypatch.setattr(ks.settings, "kubernetes_storage_class", "default-sc")
    vct = B._build_volume_claim_templates({"storage": "1Gi"})
    assert vct[0]["spec"]["storageClassName"] == "default-sc"


# ---------------------------------------------------------------------------
# _build_sandbox
# ---------------------------------------------------------------------------
def test_build_sandbox_shape():
    sb = B._build_sandbox("u1", "default", {}, "sk-key")
    assert sb["apiVersion"] == f"{ks.settings.sandbox_core_group}/{ks.settings.sandbox_version}"
    assert sb["kind"] == "Sandbox"
    assert sb["metadata"]["name"] == _sandbox_name("u1")
    assert sb["metadata"]["namespace"] == ks.settings.kubernetes_namespace
    assert sb["spec"]["service"] is True
    assert "volumeClaimTemplates" not in sb["spec"]        # ephemeral


def test_build_sandbox_annotations_carry_exact_identity():
    sb = B._build_sandbox("user@corp", "My Team!", {}, "sk-key")
    ann = sb["metadata"]["annotations"]
    assert ann[_ANN_USER_ID] == "user@corp"
    assert ann[_ANN_POLICY_ID] == "My Team!"               # verbatim, not slugged


def test_build_sandbox_adds_vct_with_storage():
    sb = B._build_sandbox("u1", "default", {"storage": "2Gi"}, "k")
    assert sb["spec"]["volumeClaimTemplates"][0]["metadata"]["name"] == "workspace"


# ---------------------------------------------------------------------------
# Status parsing: _is_suspended / _sandbox_running / _api_key / _connection_info
# ---------------------------------------------------------------------------
def test_is_suspended():
    assert B._is_suspended(make_sandbox(suspended=True)) is True
    assert B._is_suspended(make_sandbox()) is False


def test_sandbox_running_true_when_ready_and_current():
    assert B._sandbox_running(make_sandbox(generation=3, observed=3)) is True


def test_sandbox_running_false_when_suspended():
    assert B._sandbox_running(make_sandbox(suspended=True)) is False


def test_sandbox_running_false_when_ready_stale():
    # Ready=True but observedGeneration behind generation -> not yet reconciled.
    assert B._sandbox_running(make_sandbox(generation=5, observed=4)) is False


def test_sandbox_running_false_when_not_ready():
    assert B._sandbox_running(make_sandbox(ready=False)) is False


def test_sandbox_running_false_without_ready_condition():
    sb = make_sandbox()
    sb["status"]["conditions"] = []
    assert B._sandbox_running(sb) is False


def test_sandbox_running_when_observed_generation_absent():
    # Guard is skipped (no obs data) -> trusts Ready=True. Documents current behavior.
    assert B._sandbox_running(make_sandbox(generation=2, observed=None)) is True


def test_api_key_from_sandbox():
    assert B._api_key_from_sandbox(make_sandbox(api_key="sk-xyz")) == "sk-xyz"
    sb = make_sandbox()
    sb["spec"]["podTemplate"]["spec"]["containers"][0]["env"] = []
    assert B._api_key_from_sandbox(sb) is None


def test_connection_info_prefers_service_fqdn():
    info = B._connection_info(make_sandbox(service_fqdn="term.ns.svc", api_key="sk-1"))
    assert info["host"] == "term.ns.svc"
    assert info["api_key"] == "sk-1"
    assert info["port"] == ks.settings.sandbox_port


def test_connection_info_falls_back_to_pod_ip():
    info = B._connection_info(
        make_sandbox(service_fqdn=None, pod_ips=["10.0.0.5"])
    )
    assert info["host"] == "10.0.0.5"


def test_connection_info_none_without_host():
    assert B._connection_info(make_sandbox(service_fqdn=None, pod_ips=None)) is None


def test_connection_info_none_without_api_key():
    sb = make_sandbox()
    sb["spec"]["podTemplate"]["spec"]["containers"][0]["env"] = []
    assert B._connection_info(sb) is None


# ---------------------------------------------------------------------------
# #3 — _wait_until_ready: transient errors retried, 404 terminal
# ---------------------------------------------------------------------------
async def test_wait_returns_info_when_ready(no_sleep):
    b = KubernetesSandboxBackend()
    patch_custom(b, FakeCustom(get_seq=[make_sandbox(name="term-x")]))
    info = await b._wait_until_ready("term-x", timeout=5)
    assert info and info["instance_id"] == "term-x"


async def test_wait_retries_transient_then_succeeds(no_sleep):
    b = KubernetesSandboxBackend()
    fake = FakeCustom(get_seq=[
        ApiException(status=503, reason="unavailable"),
        ApiException(status=500, reason="boom"),
        make_sandbox(name="term-x"),
    ])
    patch_custom(b, fake)
    info = await b._wait_until_ready("term-x", timeout=5)
    assert info is not None                 # survived two transient errors
    assert fake.get_calls == 3


async def test_wait_gives_up_at_deadline_without_raising(no_sleep):
    b = KubernetesSandboxBackend()
    fake = FakeCustom(get_seq=[ApiException(status=503, reason="unavailable")])
    patch_custom(b, fake)
    res = await b._wait_until_ready("term-x", timeout=0.05)
    assert res is None                      # returns None, does NOT propagate
    assert fake.get_calls > 1               # kept polling until the deadline


async def test_wait_404_is_terminal(no_sleep):
    b = KubernetesSandboxBackend()
    fake = FakeCustom(get_seq=[ApiException(status=404, reason="not found")])
    patch_custom(b, fake)
    res = await b._wait_until_ready("term-x", timeout=5)
    assert res is None
    assert fake.get_calls == 1              # gone -> stop immediately


# ---------------------------------------------------------------------------
# #4 — reconcile: re-adopt Running sandboxes after a restart
# ---------------------------------------------------------------------------
async def test_reconcile_adopts_running_sandbox():
    b = KubernetesSandboxBackend()
    sb = make_sandbox(user_id="u1", policy_id="default")
    patch_custom(b, FakeCustom(list_result={"items": [sb]}))
    await b.reconcile()
    key = b._key("u1", "default")
    assert key in b._instances
    assert b._instances[key]["instance_id"] == _sandbox_name("u1")
    assert b._specs[key] == {}              # spec not persisted -> global idle default
    assert key in b._activity


async def test_reconcile_uses_exact_policy_from_annotation():
    b = KubernetesSandboxBackend()
    # Policy id the hashed name + slugged label could not round-trip.
    sb = make_sandbox(user_id="u1", policy_id="My Team!")
    patch_custom(b, FakeCustom(list_result={"items": [sb]}))
    await b.reconcile()
    assert b._key("u1", "My Team!") in b._instances


async def test_reconcile_skips_suspended():
    b = KubernetesSandboxBackend()
    sb = make_sandbox(user_id="u1", suspended=True)
    patch_custom(b, FakeCustom(list_result={"items": [sb]}))
    await b.reconcile()
    assert b._instances == {}


async def test_reconcile_skips_when_annotation_missing():
    b = KubernetesSandboxBackend()
    sb = make_sandbox(user_id="u1")
    del sb["metadata"]["annotations"][_ANN_USER_ID]
    patch_custom(b, FakeCustom(list_result={"items": [sb]}))
    await b.reconcile()
    assert b._instances == {}


async def test_reconcile_skips_without_endpoint():
    b = KubernetesSandboxBackend()
    sb = make_sandbox(user_id="u1", service_fqdn=None, pod_ips=None)
    patch_custom(b, FakeCustom(list_result={"items": [sb]}))
    await b.reconcile()
    assert b._instances == {}


async def test_reconcile_handles_list_error_gracefully():
    b = KubernetesSandboxBackend()
    patch_custom(b, FakeCustom(list_exc=ApiException(status=500, reason="boom")))
    await b.reconcile()                     # must not raise
    assert b._instances == {}


# ---------------------------------------------------------------------------
# ensure_terminal: get-or-create, resume, fast-path cache
# ---------------------------------------------------------------------------
async def test_ensure_terminal_provisions_when_missing(no_sleep):
    b = KubernetesSandboxBackend()
    fake = FakeCustom(get_seq=[
        ApiException(status=404, reason="nf"),   # fast path
        ApiException(status=404, reason="nf"),   # re-check under lock
        make_sandbox(user_id="u1"),              # readiness poll
    ])
    patch_custom(b, fake)
    info = await b.ensure_terminal("u1")
    assert info and info["instance_id"] == _sandbox_name("u1")
    assert len(fake.created) == 1
    assert fake.created[0]["metadata"]["name"] == _sandbox_name("u1")
    assert b._key("u1", "default") in b._instances


async def test_ensure_terminal_resumes_suspended(no_sleep):
    b = KubernetesSandboxBackend()
    suspended = make_sandbox(user_id="u1", suspended=True)
    fake = FakeCustom(get_seq=[suspended, suspended, make_sandbox(user_id="u1")])
    patch_custom(b, fake)
    info = await b.ensure_terminal("u1")
    assert info is not None
    assert fake.patched == [
        (_sandbox_name("u1"), {"spec": {"operatingMode": "Running"}})
    ]
    assert fake.created == []               # resumed, not re-provisioned


async def test_ensure_terminal_serves_cache_within_ttl():
    b = KubernetesSandboxBackend()
    fake = FakeCustom()
    patch_custom(b, fake)
    key = b._key("u1", "default")
    cached = {"instance_id": _sandbox_name("u1"), "host": "h", "port": 1, "api_key": "k"}
    b._track(key, cached, {})
    assert await b.ensure_terminal("u1") == cached
    assert fake.get_calls == 0              # no API round-trip within the TTL


async def test_ensure_terminal_reverifies_after_ttl():
    b = KubernetesSandboxBackend()
    sb = make_sandbox(user_id="u1")
    fake = FakeCustom(get_seq=[sb])
    patch_custom(b, fake)
    key = b._key("u1", "default")
    b._track(key, {"instance_id": _sandbox_name("u1")}, {})
    b._verified_at[key] = 0.0               # expire the TTL
    info = await b.ensure_terminal("u1")
    assert fake.get_calls == 1
    assert info["host"] == "term.ns.svc"    # refreshed from the live object


async def test_ensure_terminal_uses_cache_on_transient_error():
    b = KubernetesSandboxBackend()
    fake = FakeCustom(get_seq=[ApiException(status=503, reason="unavailable")])
    patch_custom(b, fake)
    key = b._key("u1", "default")
    cached = {"instance_id": _sandbox_name("u1"), "host": "h", "port": 1, "api_key": "k"}
    b._track(key, cached, {})
    b._verified_at[key] = 0.0               # force re-verify -> API error
    assert await b.ensure_terminal("u1") == cached


# ---------------------------------------------------------------------------
# provision / start / teardown / _reap_idle
# ---------------------------------------------------------------------------
async def test_provision_resumes_suspended_on_conflict(no_sleep):
    # 409 hands back a Suspended sandbox -> must resume, not wait out the timeout.
    b = KubernetesSandboxBackend()
    fake = FakeCustom(
        create_exc=ApiException(status=409, reason="conflict"),
        get_seq=[make_sandbox(user_id="u1", suspended=True), make_sandbox(user_id="u1")],
    )
    patch_custom(b, fake)
    info = await b.provision("u1")
    assert info is not None
    assert fake.patched == [
        (_sandbox_name("u1"), {"spec": {"operatingMode": "Running"}})
    ]


async def test_start_resumes_suspended_and_is_idempotent():
    b = KubernetesSandboxBackend()
    fake = FakeCustom(get_seq=[make_sandbox(user_id="u1", suspended=True)])
    patch_custom(b, fake)
    assert await b.start(_sandbox_name("u1")) is True
    assert len(fake.patched) == 1

    fake2 = FakeCustom(get_seq=[make_sandbox(user_id="u1")])
    patch_custom(b, fake2)
    assert await b.start(_sandbox_name("u1")) is True
    assert fake2.patched == []              # already running -> no patch

    fake3 = FakeCustom(get_seq=[ApiException(status=404, reason="nf")])
    patch_custom(b, fake3)
    assert await b.start(_sandbox_name("u1")) is False


async def test_teardown_deletes_sandbox():
    b = KubernetesSandboxBackend()
    fake = FakeCustom()
    patch_custom(b, fake)
    await b.teardown("term-x")
    assert fake.deleted == ["term-x"]


async def test_reap_idle_suspends_and_untracks():
    b = KubernetesSandboxBackend()
    fake = FakeCustom()
    patch_custom(b, fake)
    key = b._key("u1", "default")
    b._track(key, {"instance_id": "term-x", "instance_name": "term-x"}, {"idle_timeout_minutes": 1})
    b._activity[key] -= 120                 # idle past the 1-minute timeout
    await b._reap_idle()
    assert fake.patched == [("term-x", {"spec": {"operatingMode": "Suspended"}})]
    assert key not in b._instances
    assert key not in b._verified_at
    assert fake.deleted == []               # suspended, never deleted


async def test_reap_idle_keeps_active_instances():
    b = KubernetesSandboxBackend()
    fake = FakeCustom()
    patch_custom(b, fake)
    key = b._key("u1", "default")
    b._track(key, {"instance_id": "term-x"}, {"idle_timeout_minutes": 1})
    await b._reap_idle()                    # just active -> untouched
    assert fake.patched == []
    assert key in b._instances
