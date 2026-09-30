"""
Host-side tests for competition.py — run on a PC, no Pico needed.

    python test_competition.py

The library targets MicroPython, so the modules it imports (`network`,
`umqtt.simple`, `ubinascii`) and the MicroPython-only `time.ticks_*` helpers do
not exist on CPython. This file installs stand-ins for them before importing
competition.py, then drives `_on_message` with the exact JSON the server sends.

What this can prove: the topic routing, the broadcast latch, deduplication, the
PONG contract, heartbeat contents. What it cannot: WiFi, real MQTT, or timing on
the actual hardware — that is tested on the bench.
"""
import json
import sys
import time
import types

# ── MicroPython stand-ins, installed before competition.py is imported ───────

_fake_network = types.ModuleType("network")
_fake_network.STA_IF = 0


class _FakeWLAN:
    def __init__(self, _iface):
        self._connected = True

    def active(self, _on):
        pass

    def isconnected(self):
        return self._connected

    def connect(self, _ssid, _pw):
        self._connected = True

    def config(self, what):
        assert what == "mac"
        return b"\xaa\xbb\xcc\xdd\xee\xff"

    def ifconfig(self):
        return ("192.168.1.99", "255.255.255.0", "192.168.1.1", "192.168.1.1")


_fake_network.WLAN = _FakeWLAN
sys.modules["network"] = _fake_network

_fake_ubinascii = types.ModuleType("ubinascii")
_fake_ubinascii.hexlify = lambda data, sep: (
    sep.join(b"%02x" % b for b in data) if isinstance(sep, bytes)
    else sep.join("%02x" % b for b in data).encode()
)
sys.modules["ubinascii"] = _fake_ubinascii


class _FakeMQTTClient:
    """Records what was published; never touches a network."""

    def __init__(self, client_id, server, port, user, password, keepalive):
        self.client_id = client_id
        self.published = []      # list of (topic, decoded payload dict)
        self.subscribed = []
        self._callback = None

    def set_callback(self, cb):
        self._callback = cb

    def connect(self):
        pass

    def subscribe(self, topic):
        self.subscribed.append(topic)

    def publish(self, topic, payload):
        self.published.append((topic, json.loads(payload)))

    def check_msg(self):
        pass


_fake_umqtt = types.ModuleType("umqtt")
_fake_umqtt_simple = types.ModuleType("umqtt.simple")
_fake_umqtt_simple.MQTTClient = _FakeMQTTClient
_fake_umqtt.simple = _fake_umqtt_simple
sys.modules["umqtt"] = _fake_umqtt
sys.modules["umqtt.simple"] = _fake_umqtt_simple

# MicroPython's monotonic-tick helpers.
time.ticks_ms = lambda: int(time.monotonic() * 1000)
time.ticks_diff = lambda a, b: a - b
time.ticks_add = lambda a, b: a + b
time.sleep_ms = lambda ms: time.sleep(ms / 1000)

from competition import FIRMWARE_VERSION, Competition   # noqa: E402

MAC = "AA:BB:CC:DD:EE:FF"
ROBOT_CMD = "robosteam/robot/%s/cmd" % MAC
BROADCAST = "robosteam/competition/7/cmd"
STATUS = "robosteam/robot/%s/status" % MAC

failures = []
checks = 0


def check(label, condition, detail=""):
    global checks
    checks += 1
    if condition:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s %s" % (label, detail))
        failures.append(label)


def new_comp():
    """A connected Competition with a fake broker, plus the started/stopped log."""
    comp = Competition(
        ssid="x", password="x", broker="192.168.1.11", port=1883,
        mqtt_user="test-mqtt-user", mqtt_password="test-mqtt-password",
        competition_id=7, friendly_name="Test Bot",
    )
    events = []
    comp.on_start(lambda run_id: events.append(("START", run_id)))
    comp.on_stop(lambda run_id: events.append(("STOP", run_id)))
    comp.connect()
    comp._client.published.clear()      # drop the connect-time heartbeat
    return comp, events


def send(comp, topic, payload):
    comp._on_message(topic.encode(), json.dumps(payload).encode())


# ── the tests ────────────────────────────────────────────────────────────────

def test_connect_builds_topics():
    comp, _ = new_comp()
    check("MAC is formatted as the server stores it", comp.mac == MAC, comp.mac)
    check("subscribes to its own command topic", ROBOT_CMD in comp._client.subscribed)
    check("subscribes to the broadcast", BROADCAST in comp._client.subscribed)


def test_unbound_robot_obeys_the_broadcast():
    """Backwards compatibility: a robot nobody has bound still works."""
    comp, events = new_comp()
    send(comp, BROADCAST, {"cmd": "START", "run_id": 5})
    check("unbound robot starts on the broadcast", events == [("START", 5)], events)
    check("running is True", comp.running is True)
    check("bound_to_team is still False", comp.bound_to_team is False)


def test_targeted_command_latches_the_binding():
    comp, events = new_comp()
    send(comp, ROBOT_CMD, {"cmd": "START", "run_id": 5})
    check("targeted START runs the callback", events == [("START", 5)], events)
    check("bound_to_team flips to True", comp.bound_to_team is True)


def test_bound_robot_ignores_another_teams_broadcast():
    """The whole point of Phase 5.21: this is the bug, in the firmware half."""
    comp, events = new_comp()
    send(comp, ROBOT_CMD, {"cmd": "START", "run_id": 5})
    send(comp, ROBOT_CMD, {"cmd": "STOP", "run_id": 5, "reason": "finished"})
    events.clear()

    # Another team is sent off. Before this change, our robot drove away.
    send(comp, BROADCAST, {"cmd": "START", "run_id": 6})
    check("BOUND ROBOT IGNORES ANOTHER TEAM'S START", events == [], events)
    check("still not running", comp.running is False)


def test_duplicate_start_fires_once():
    """A bound robot gets START on both topics milliseconds apart."""
    comp, events = new_comp()
    send(comp, ROBOT_CMD, {"cmd": "START", "run_id": 5})
    send(comp, BROADCAST, {"cmd": "START", "run_id": 5})
    check("on_start fires once, not twice", events == [("START", 5)], events)


def test_duplicate_start_fires_once_broadcast_first():
    """Delivery order is not guaranteed — the broadcast can arrive first."""
    comp, events = new_comp()
    send(comp, BROADCAST, {"cmd": "START", "run_id": 5})
    send(comp, ROBOT_CMD, {"cmd": "START", "run_id": 5})
    check("on_start still fires once", events == [("START", 5)], events)
    check("the targeted copy still latches the binding", comp.bound_to_team is True)


def test_broadcast_stop_without_run_id_is_deduplicated():
    """The broadcast STOP carries no run_id; the targeted one does."""
    comp, events = new_comp()
    send(comp, ROBOT_CMD, {"cmd": "START", "run_id": 5})
    events.clear()
    send(comp, BROADCAST, {"cmd": "STOP"})
    send(comp, ROBOT_CMD, {"cmd": "STOP", "run_id": 5})
    check("on_stop fires once despite the two spellings",
          events == [("STOP", None)] or events == [("STOP", 5)], events)
    check("running is cleared", comp.running is False)


def test_stop_reason_is_exposed():
    comp, _ = new_comp()
    send(comp, ROBOT_CMD, {"cmd": "START", "run_id": 5})
    check("stop_reason clears on START", comp.stop_reason is None)

    send(comp, ROBOT_CMD, {"cmd": "STOP", "run_id": 5, "reason": "finished"})
    check("stop_reason is 'finished'", comp.stop_reason == "finished", comp.stop_reason)

    send(comp, ROBOT_CMD, {"cmd": "START", "run_id": 6})
    send(comp, ROBOT_CMD, {"cmd": "STOP", "run_id": 6, "reason": "timeout"})
    check("stop_reason is 'timeout'", comp.stop_reason == "timeout", comp.stop_reason)

    send(comp, ROBOT_CMD, {"cmd": "START", "run_id": 7})
    send(comp, ROBOT_CMD, {"cmd": "STOP", "run_id": 7})
    check("a judge's hand-void leaves stop_reason None", comp.stop_reason is None,
          comp.stop_reason)


def test_ping_is_answered_with_the_nonce():
    comp, _ = new_comp()
    send(comp, ROBOT_CMD, {"cmd": "PING", "nonce": "9c302b032ef6458c"})

    published = comp._client.published
    check("exactly one reply", len(published) == 1, published)
    topic, payload = published[0]
    check("PONG goes to the status topic", topic == STATUS, topic)
    check("cmd is PONG", payload.get("cmd") == "PONG", payload)
    check("nonce is echoed unchanged",
          payload.get("nonce") == "9c302b032ef6458c", payload)
    check("firmware version is reported",
          payload.get("firmware") == FIRMWARE_VERSION, payload)
    check("device_id identifies the robot", payload.get("device_id") == MAC, payload)


def test_ping_does_not_latch_or_disturb_a_run():
    """A student tests an unbound robot, and tests one mid-run."""
    comp, events = new_comp()
    send(comp, ROBOT_CMD, {"cmd": "PING", "nonce": "abc"})
    check("PING does not claim the robot is bound", comp.bound_to_team is False)

    send(comp, BROADCAST, {"cmd": "START", "run_id": 5})
    events.clear()
    send(comp, ROBOT_CMD, {"cmd": "PING", "nonce": "def"})
    check("PING mid-run does not stop the robot", comp.running is True)
    check("PING fires no start/stop callback", events == [], events)


def test_heartbeat_reports_firmware():
    comp, _ = new_comp()
    comp._publish_heartbeat()
    _, payload = comp._client.published[-1]
    check("heartbeat carries the firmware version",
          payload.get("firmware") == FIRMWARE_VERSION, payload)
    check("heartbeat carries the friendly name",
          payload.get("friendly_name") == "Test Bot", payload)


def test_bad_json_and_unknown_commands_are_survivable():
    comp, events = new_comp()
    comp._on_message(ROBOT_CMD.encode(), b"not json at all")
    send(comp, ROBOT_CMD, {"cmd": "WOBBLE"})
    check("garbage does not crash the robot or start it",
          events == [] and comp.running is False, events)


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print("\n%s" % name)
            fn()

    print("\n%d/%d checks passed" % (checks - len(failures), checks))
    if failures:
        print("FAILED: " + ", ".join(failures))
        return 1
    print("ALL PICOBOT LIBRARY CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
