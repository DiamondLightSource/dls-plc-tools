"""Unit tests for the kubectl port-forward layer, with subprocess mocked out
(kubectl/klogin only exist on DLS workstations)."""

import json
from types import SimpleNamespace

import pytest

from dls_plc_tools import portforward
from dls_plc_tools.portforward import PortForward, PortForwardError, name_for


def _ok(stdout=""):
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


class _NullConn:
    """Stand-in for a socket returned by create_connection (a context manager)."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeRun:
    """Records kubectl invocations and replays canned results.

    Each call is matched by the verb (apply/get/delete); the applied manifest
    JSON is captured from stdin so tests can assert on it.
    """

    def __init__(self, get_stdout="True"):
        self.calls = []
        self.applied: dict = {}
        self._get_stdout = get_stdout

    def __call__(self, cmd, input=None, capture_output=True, text=True, timeout=30):
        # cmd is ["bash", "-c", script, "bash", *kubectl_args]
        args = cmd[4:]
        self.calls.append(args)
        verb = args[0]
        if verb == "apply":
            assert input is not None
            self.applied = json.loads(input)
            return _ok()
        if verb == "get":
            return _ok(self._get_stdout)
        return _ok()  # delete


@pytest.fixture(autouse=True)
def _fixed_user(monkeypatch):
    monkeypatch.setattr(portforward.getpass, "getuser", lambda: "tester")


@pytest.fixture(autouse=True)
def _reachable(monkeypatch):
    """By default the post-Ready TCP reachability probe succeeds instantly, so
    tests exercise the kubectl path without a real socket. Tests that care about
    the probe override this."""

    monkeypatch.setattr(
        portforward.socket, "create_connection", lambda *a, **k: _NullConn()
    )


def test_name_for():
    assert name_for(44818) == "pfwd-tester-44818"


def test_resolve_host_passes_through_literal_ip(monkeypatch):
    # a literal IP resolves to itself (gethostbyname is identity on an IP)
    monkeypatch.setattr(portforward.socket, "gethostbyname", lambda v: v, raising=True)
    assert portforward.resolve_host(" 172.23.206.169 ") == "172.23.206.169"


def test_resolve_host_looks_up_dns_name(monkeypatch):
    monkeypatch.setattr(
        portforward.socket,
        "gethostbyname",
        lambda v: "172.23.206.169" if v == "sr19c-pfwd" else "0.0.0.0",
    )
    assert portforward.resolve_host("sr19c-pfwd") == "172.23.206.169"


def test_resolve_host_raises_on_failure(monkeypatch):
    def boom(_v):
        raise OSError("Name or service not known")

    monkeypatch.setattr(portforward.socket, "gethostbyname", boom)
    with pytest.raises(PortForwardError, match="could not resolve service IP"):
        portforward.resolve_host("nope-pfwd")


def test_up_applies_pod_and_service(monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(portforward.subprocess, "run", fake)

    pf = PortForward()
    name = pf.up("192.168.8.10", "sr08c-node.example", "172.23.206.158", 44818)

    assert name == "pfwd-tester-44818"
    items = {i["kind"]: i for i in fake.applied["items"]}
    pod, service = items["Pod"], items["Service"]

    assert pod["spec"]["nodeName"] == "sr08c-node.example"  # node used verbatim
    # tolerate the per-cell location taint so a NoExecute taint can't evict us
    assert {"key": "location", "operator": "Exists"} in pod["spec"]["tolerations"]
    assert pod["spec"]["containers"][0]["args"] == [
        "tcp-l:44818,fork,reuseaddr",
        "tcp:192.168.8.10:44818",
    ]
    assert service["spec"]["loadBalancerIP"] == "172.23.206.158"
    assert service["spec"]["ports"][0]["port"] == 44818
    assert service["spec"]["ports"][0]["targetPort"] == 44818
    # shares the fixed IP with the permanent forward that owns it (keyed on the IP)
    assert (
        service["metadata"]["annotations"]["metallb.universe.tf/allow-shared-ip"]
        == "172.23.206.158"
    )


def test_up_deletes_stale_forward_first(monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(portforward.subprocess, "run", fake)

    PortForward().up("192.168.8.10", "node", "172.23.206.158", 44818)

    verbs = [c[0] for c in fake.calls]
    # stale pod + service deleted before the apply
    assert verbs.index("delete") < verbs.index("apply")


def test_up_custom_port_threads_through(monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(portforward.subprocess, "run", fake)

    name = PortForward().up("192.168.8.10", "node", "172.23.206.158", 2222)

    assert name == "pfwd-tester-2222"
    service = next(i for i in fake.applied["items"] if i["kind"] == "Service")
    assert service["spec"]["ports"][0]["port"] == 2222


def test_up_waits_for_reachability(monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(portforward.subprocess, "run", fake)

    probed = []
    monkeypatch.setattr(
        portforward.socket,
        "create_connection",
        lambda addr, timeout=None: probed.append(addr) or _NullConn(),
    )

    PortForward().up("192.168.8.10", "node", "172.23.206.158", 44818)
    # gated on the address aphyt will actually connect to
    assert probed == [("172.23.206.158", 44818)]


def test_up_raises_when_never_reachable(monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(portforward.subprocess, "run", fake)
    monkeypatch.setattr(portforward, "REACHABLE_TIMEOUT", 0.0)  # fail fast
    monkeypatch.setattr(portforward.time, "sleep", lambda _s: None)

    def refused(addr, timeout=None):
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(portforward.socket, "create_connection", refused)

    with pytest.raises(PortForwardError, match="not reachable"):
        PortForward().up("192.168.8.10", "node", "172.23.206.158", 44818)


def test_up_raises_when_pod_never_ready(monkeypatch):
    fake = FakeRun(get_stdout="False")
    monkeypatch.setattr(portforward.subprocess, "run", fake)
    monkeypatch.setattr(portforward, "READY_TIMEOUT", 0.0)  # fail fast
    monkeypatch.setattr(portforward.time, "sleep", lambda _s: None)

    with pytest.raises(PortForwardError, match="not ready"):
        PortForward().up("192.168.8.10", "node", "172.23.206.158", 44818)


def test_down_cleans_up_after_failed_up(monkeypatch):
    # up() creates the pod/service then times out waiting for Ready; down() must
    # still delete them (name recorded before the apply), or the forward leaks.
    fake = FakeRun(get_stdout="False")  # pod never becomes Ready
    monkeypatch.setattr(portforward.subprocess, "run", fake)
    monkeypatch.setattr(portforward, "READY_TIMEOUT", 0.0)  # fail fast
    monkeypatch.setattr(portforward.time, "sleep", lambda _s: None)

    pf = PortForward()
    with pytest.raises(PortForwardError):
        pf.up("192.168.8.10", "badnode", "172.23.206.158", 44818)

    fake.calls.clear()
    pf.down()
    deleted = [c for c in fake.calls if c[0] == "delete"]
    assert [c[1] for c in deleted] == ["pod", "service"]
    assert all("pfwd-tester-44818" in c for c in deleted)
    # the pod must be force-deleted: a stray pod bound to a non-existent node
    # (the failing case here) sticks in Terminating under a graceful delete
    pod_delete = next(c for c in deleted if c[1] == "pod")
    assert "--force" in pod_delete and "--grace-period=0" in pod_delete


def test_up_reports_events_when_pod_disappears(monkeypatch):
    # get pod -> NotFound (evicted), but get events still returns the reason.
    def run(cmd, input=None, capture_output=True, text=True, timeout=30):
        args = cmd[4:]
        if args[:2] == ["get", "pod"]:
            return SimpleNamespace(returncode=1, stdout="", stderr='pods "x" not found')
        if args[:2] == ["get", "events"]:
            return _ok("TaintManagerEviction  Marking for deletion, untolerated taint")
        return _ok()

    monkeypatch.setattr(portforward.subprocess, "run", run)

    with pytest.raises(PortForwardError) as excinfo:
        PortForward().up("192.168.8.10", "node", "172.23.206.158", 44818)
    msg = str(excinfo.value)
    assert "disappeared before becoming ready" in msg
    assert "TaintManagerEviction" in msg  # the eviction reason is surfaced


def test_kubectl_silences_klogin_banner(monkeypatch):
    # klogin's welcome banner must not reach captured kubectl stdout, or exact
    # parses (e.g. _wait_ready's "True" check) break when it appears.
    scripts = []

    def run(cmd, input=None, capture_output=True, text=True, timeout=30):
        scripts.append(cmd[2])  # the bash -c script
        return _ok("True")

    monkeypatch.setattr(portforward.subprocess, "run", run)
    portforward._kubectl(["get", "pod", "x"])

    assert scripts, "expected a kubectl invocation"
    # klogin's stdout is redirected away before kubectl runs
    assert 'source "' in scripts[0] and ">/dev/null" in scripts[0]


def test_kubectl_failure_raises_with_stderr(monkeypatch):
    def failing(cmd, input=None, capture_output=True, text=True, timeout=30):
        return SimpleNamespace(returncode=1, stdout="", stderr="boom: forbidden")

    monkeypatch.setattr(portforward.subprocess, "run", failing)

    with pytest.raises(PortForwardError, match="boom: forbidden"):
        PortForward().up("192.168.8.10", "node", "172.23.206.158", 44818)


def test_down_deletes_and_is_idempotent(monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(portforward.subprocess, "run", fake)

    pf = PortForward()
    pf.up("192.168.8.10", "node", "172.23.206.158", 44818)
    fake.calls.clear()
    pf.down()

    deleted = [c for c in fake.calls if c[0] == "delete"]
    assert [c[1] for c in deleted] == ["pod", "service"]
    assert all("pfwd-tester-44818" in c for c in deleted)

    fake.calls.clear()
    pf.down()  # nothing to tear down now
    assert fake.calls == []


def test_down_without_up_is_noop(monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(portforward.subprocess, "run", fake)

    PortForward().down()
    assert fake.calls == []
