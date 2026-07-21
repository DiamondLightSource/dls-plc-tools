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

    def __call__(
        self, cmd, input=None, stdin=None, capture_output=True, text=True, timeout=30
    ):
        # cmd is ["bash", "-c", script, "bash", *kubectl_args]
        args = cmd[4:]
        self.calls.append(args)
        verb = args[0]
        if verb == "auth":
            return _ok()  # `auth whoami` preflight: authenticated
        if verb == "apply":
            assert input is not None
            self.applied = json.loads(input)
            return _ok()
        if verb == "get":
            if "svc" in args:
                return _ok('{"items": []}')  # _check_free: no conflicting service
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
    def run(cmd, input=None, stdin=None, capture_output=True, text=True, timeout=30):
        args = cmd[4:]
        if args[:2] == ["get", "svc"]:
            return _ok('{"items": []}')  # _check_free: nothing else claims the IP
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

    def run(cmd, input=None, stdin=None, capture_output=True, text=True, timeout=30):
        scripts.append(cmd[2])  # the bash -c script
        return _ok("True")

    monkeypatch.setattr(portforward.subprocess, "run", run)
    portforward._kubectl(["get", "pod", "x"])

    assert scripts, "expected a kubectl invocation"
    # klogin's stdout is redirected away before kubectl runs
    assert 'source "' in scripts[0] and ">/dev/null" in scripts[0]


def test_kubectl_failure_raises_with_stderr(monkeypatch):
    def failing(
        cmd, input=None, stdin=None, capture_output=True, text=True, timeout=30
    ):
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


def test_up_aborts_when_not_logged_in(monkeypatch):
    # The preflight `auth whoami` fails -> up() must abort with a login message
    # and never create the pod/service.
    def run(cmd, input=None, stdin=None, capture_output=True, text=True, timeout=30):
        args = cmd[4:]
        if args[:2] == ["auth", "whoami"]:
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="error: You must be logged in to the server (Unauthorized)",
            )
        raise AssertionError(f"should not run {args} when not logged in")

    monkeypatch.setattr(portforward.subprocess, "run", run)

    with pytest.raises(PortForwardError, match="not logged into acastus"):
        PortForward().up("192.168.8.10", "node", "172.23.206.158", 44818)


def test_check_auth_passes_when_logged_in(monkeypatch):
    calls = []

    def run(cmd, input=None, stdin=None, capture_output=True, text=True, timeout=30):
        calls.append(cmd[4:])
        return _ok("Username  jjc62351")

    monkeypatch.setattr(portforward.subprocess, "run", run)
    portforward.check_auth()  # must not raise
    assert calls == [["auth", "whoami"]]


def test_kubectl_timeout_is_wrapped(monkeypatch):
    # A stalled kubectl (e.g. kubelogin blocking on a password prompt) must
    # surface as a clean PortForwardError, not a raw TimeoutExpired.
    def stall(cmd, input=None, stdin=None, capture_output=True, text=True, timeout=30):
        raise portforward.subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(portforward.subprocess, "run", stall)
    with pytest.raises(PortForwardError, match="timed out"):
        portforward._kubectl(["get", "pod", "x"])


def test_list_nodes_strips_node_prefix(monkeypatch):
    def run(cmd, input=None, stdin=None, capture_output=True, text=True, timeout=30):
        args = cmd[4:]
        if args[:2] == ["auth", "whoami"]:
            return _ok()  # authenticated
        assert args == ["get", "nodes", "-o", "name"]
        return _ok("node/sr01c-k8s-serv-01.example\nnode/sr02c-k8s-serv-01.example\n")

    monkeypatch.setattr(portforward.subprocess, "run", run)
    assert portforward.list_nodes() == [
        "sr01c-k8s-serv-01.example",
        "sr02c-k8s-serv-01.example",
    ]


def test_list_nodes_reports_login_when_logged_out(monkeypatch):
    # A logged-out node fetch gives the friendly "log in" message, not a raw
    # kubectl error, and never queries nodes.
    def run(cmd, input=None, stdin=None, capture_output=True, text=True, timeout=30):
        args = cmd[4:]
        if args[:2] == ["auth", "whoami"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="Unauthorized")
        raise AssertionError(f"should not query {args} when logged out")

    monkeypatch.setattr(portforward.subprocess, "run", run)
    with pytest.raises(PortForwardError, match="not logged into acastus"):
        portforward.list_nodes()


def test_up_aborts_when_service_ip_port_taken(monkeypatch):
    # Another service already claims service_ip:44818 -> abort before creating
    # anything, naming the conflicting service.
    other = {
        "metadata": {"name": "pfwd-someone-44818"},
        "spec": {"loadBalancerIP": "172.23.206.158", "ports": [{"port": 44818}]},
    }

    def run(cmd, input=None, stdin=None, capture_output=True, text=True, timeout=30):
        args = cmd[4:]
        if args[:2] == ["auth", "whoami"]:
            return _ok()
        if args[:2] == ["get", "svc"]:
            return _ok(json.dumps({"items": [other]}))
        raise AssertionError(f"should not reach {args} when the port is taken")

    monkeypatch.setattr(portforward.subprocess, "run", run)
    with pytest.raises(PortForwardError, match="already forwarded by service"):
        PortForward().up("192.168.8.10", "node", "172.23.206.158", 44818)


def test_check_free_ignores_own_forward_and_other_ports(monkeypatch):
    # Our own same-named stale forward and the permanent forward sharing the IP
    # on other ports must NOT count as conflicts, so up() proceeds to apply.
    items = [
        {  # our own stale forward (same name) — replaced by up(), not a conflict
            "metadata": {"name": "pfwd-tester-44818"},
            "spec": {"loadBalancerIP": "172.23.206.158", "ports": [{"port": 44818}]},
        },
        {  # permanent per-cell forward: same IP, terminal-server port
            "metadata": {"name": "sr08c-pfwd"},
            "spec": {"loadBalancerIP": "172.23.206.158", "ports": [{"port": 8026}]},
        },
    ]

    def run(cmd, input=None, stdin=None, capture_output=True, text=True, timeout=30):
        args = cmd[4:]
        if args[:2] == ["auth", "whoami"]:
            return _ok()
        if args[:2] == ["get", "svc"]:
            return _ok(json.dumps({"items": items}))
        if args[:2] == ["get", "pod"]:
            return _ok("True")
        return _ok()

    monkeypatch.setattr(portforward.subprocess, "run", run)
    # must not raise
    PortForward().up("192.168.8.10", "node", "172.23.206.158", 44818)


def test_cluster_selects_klogin_script(monkeypatch):
    scripts = []

    def run(cmd, input=None, stdin=None, capture_output=True, text=True, timeout=30):
        scripts.append(cmd[2])  # the bash -c script
        return _ok("ok")

    monkeypatch.setattr(portforward.subprocess, "run", run)
    portforward._kubectl(["get", "pod", "x"], cluster="tuscana")
    assert f"{portforward.KLOGIN_DIR}/tuscana" in scripts[0]


def test_up_threads_cluster_through_every_call(monkeypatch):
    scripts = []

    def run(cmd, input=None, stdin=None, capture_output=True, text=True, timeout=30):
        scripts.append(cmd[2])
        verb = cmd[4]
        if verb == "get" and cmd[5] == "svc":
            return _ok('{"items": []}')
        if verb == "get" and cmd[5] == "pod":
            return _ok("True")
        if verb == "apply":
            return _ok()
        return _ok()

    monkeypatch.setattr(portforward.subprocess, "run", run)
    PortForward(cluster="tuscana").up("192.168.8.10", "node", "172.23.206.158", 44818)
    assert scripts, "expected kubectl calls"
    assert all(f"{portforward.KLOGIN_DIR}/tuscana" in s for s in scripts)


def test_kubectl_forces_noninteractive_stdin(monkeypatch):
    # With no piped input, stdin must be DEVNULL so kubelogin can't block on an
    # interactive fedid/password prompt.
    seen = {}

    def run(cmd, input=None, stdin=None, capture_output=True, text=True, timeout=30):
        seen["input"], seen["stdin"] = input, stdin
        return _ok("ok")

    monkeypatch.setattr(portforward.subprocess, "run", run)
    portforward._kubectl(["get", "pod", "x"])
    assert seen["input"] is None
    assert seen["stdin"] == portforward.subprocess.DEVNULL
