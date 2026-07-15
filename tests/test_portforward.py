"""Unit tests for the kubectl port-forward layer, with subprocess mocked out
(kubectl/klogin only exist on DLS workstations)."""

import json
from types import SimpleNamespace

import pytest

from dls_plc_tools import portforward
from dls_plc_tools.portforward import PortForward, PortForwardError, name_for


def _ok(stdout=""):
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


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


def test_name_for():
    assert name_for(44818) == "pfwd-tester-44818"


def test_up_applies_pod_and_service(monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(portforward.subprocess, "run", fake)

    pf = PortForward()
    name = pf.up("192.168.8.10", "sr08c-node.example", "172.23.206.158", 44818)

    assert name == "pfwd-tester-44818"
    items = {i["kind"]: i for i in fake.applied["items"]}
    pod, service = items["Pod"], items["Service"]

    assert pod["spec"]["nodeName"] == "sr08c-node.example"  # node used verbatim
    assert pod["spec"]["containers"][0]["args"] == [
        "tcp-l:44818,fork,reuseaddr",
        "tcp:192.168.8.10:44818",
    ]
    assert service["spec"]["loadBalancerIP"] == "172.23.206.158"
    assert service["spec"]["ports"][0]["port"] == 44818
    assert service["spec"]["ports"][0]["targetPort"] == 44818


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


def test_up_raises_when_pod_never_ready(monkeypatch):
    fake = FakeRun(get_stdout="False")
    monkeypatch.setattr(portforward.subprocess, "run", fake)
    monkeypatch.setattr(portforward, "READY_TIMEOUT", 0.0)  # fail fast
    monkeypatch.setattr(portforward.time, "sleep", lambda _s: None)

    with pytest.raises(PortForwardError, match="not ready"):
        PortForward().up("192.168.8.10", "node", "172.23.206.158", 44818)


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
