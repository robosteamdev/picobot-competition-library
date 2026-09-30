#!/usr/bin/env python3
"""
Run the REAL competition.py against the REAL broker — no Pico, no hardware.

`test_competition.py` drives `_on_message` directly with a fake client. Useful,
but it proves the branching, not the wiring: it never opens a socket, never
subscribes to a topic, and never sees a message the server actually sent.

This runs the unmodified library against the real Mosquitto broker, with a
`umqtt.simple`-shaped shim over paho. Everything the library does — building
topic names from the MAC, subscribing, decoding what arrives, deciding whether a
command is for it — happens for real. What is stubbed is only what a PC does not
have: WiFi and the MicroPython tick helpers.

That leaves the bench session to debug WiFi, GPIO and timing, rather
than discovering a protocol mistake with a soldering iron in hand.

    # on a computer that can reach the broker:
    python3 live_broker_check.py --broker <broker-address> --port 1883 \
        --user <mqtt-user> --password <mqtt-password>

Pair it with tests/live_targeted_start_check.py in the competitions repo: that
one drives a real judge START through the server. This one is the robot at the
other end of the same wire.
"""
import argparse
import json
import sys
import time
import types

import paho.mqtt.client as mqtt

MAC = 'AA:BB:CC:DD:EE:FF'
COMPETITION_ID = 7

failures = []
checks = 0


def check(label, condition, detail=''):
    global checks
    checks += 1
    print(f'  {"ok  " if condition else "FAIL"} {label}{"" if condition else "  " + str(detail)}')
    if not condition:
        failures.append(label)


# ── MicroPython stand-ins ────────────────────────────────────────────────────
# Only what a PC genuinely lacks. The MQTT layer below is real.

_net = types.ModuleType('network')
_net.STA_IF = 0


class _WLAN:
    def __init__(self, _i): pass
    def active(self, _o): pass
    def isconnected(self): return True
    def connect(self, *_): pass
    def config(self, what):
        assert what == 'mac'
        return bytes(int(b, 16) for b in MAC.split(':'))
    def ifconfig(self): return ('127.0.0.1', '255.255.255.0', '127.0.0.1', '127.0.0.1')


_net.WLAN = _WLAN
sys.modules['network'] = _net

_ub = types.ModuleType('ubinascii')


def _hexlify(data, sep):
    # MicroPython accepts a str separator here and returns bytes; the library
    # passes ":" and then calls .decode() on the result. CPython's binascii is
    # stricter, so normalise rather than make the library work around a stub.
    if isinstance(sep, str):
        sep = sep.encode()
    return sep.join(b'%02x' % b for b in data)


_ub.hexlify = _hexlify
sys.modules['ubinascii'] = _ub

time.ticks_ms = lambda: int(time.monotonic() * 1000)
time.ticks_diff = lambda a, b: a - b
time.ticks_add = lambda a, b: a + b
time.sleep_ms = lambda ms: time.sleep(ms / 1000)


class RealMQTTClient:
    """`umqtt.simple.MQTTClient` on top of paho — same five methods the library uses.

    umqtt is callback-per-poll: `check_msg()` pumps the socket and invokes the
    callback for anything waiting. paho's loop is threaded, so incoming messages
    are queued here and `check_msg()` drains that queue, which keeps the
    library's own control flow untouched.
    """

    def __init__(self, client_id, server, port, user, password, keepalive):
        self._queue = []
        self._cb = None
        self._c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                              client_id=client_id.decode() if isinstance(client_id, bytes)
                              else client_id)
        self._c.username_pw_set(user, password)
        self._c.on_message = lambda _c, _u, m: self._queue.append((m.topic, m.payload))
        self._server, self._port, self._keepalive = server, port, keepalive

    def set_callback(self, cb):
        self._cb = cb

    def connect(self):
        self._c.connect(self._server, self._port, self._keepalive)
        self._c.loop_start()

    def subscribe(self, topic):
        self._c.subscribe(topic if isinstance(topic, str) else topic.decode(), qos=1)

    def publish(self, topic, payload):
        self._c.publish(topic if isinstance(topic, str) else topic.decode(), payload, qos=1)

    def check_msg(self):
        while self._queue:
            topic, payload = self._queue.pop(0)
            if self._cb:
                self._cb(topic.encode(), payload)

    def disconnect(self):
        self._c.loop_stop()
        self._c.disconnect()


_umqtt = types.ModuleType('umqtt')
_simple = types.ModuleType('umqtt.simple')
_simple.MQTTClient = RealMQTTClient
_umqtt.simple = _simple
sys.modules['umqtt'] = _umqtt
sys.modules['umqtt.simple'] = _simple

from competition import FIRMWARE_VERSION, Competition   # noqa: E402


# ── the check ────────────────────────────────────────────────────────────────

def publisher(args):
    """A second client, standing in for the competition server."""
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='picobot-live-check')
    c.username_pw_set(args.user, args.password)
    c.connect(args.broker, args.port, 60)
    c.loop_start()
    return c


def pump(comp, seconds=2.0):
    """Poll the library the way a student's main loop does."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        comp.poll()
        time.sleep(0.02)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--broker', required=True)
    p.add_argument('--port', type=int, default=1883)
    p.add_argument('--user', required=True)
    p.add_argument('--password', required=True)
    args = p.parse_args()

    events = []
    comp = Competition(
        ssid='x', password='x', broker=args.broker, port=args.port,
        mqtt_user=args.user, mqtt_password=args.password,
        competition_id=COMPETITION_ID, friendly_name='Live Check Bot',
    )
    comp.on_start(lambda run_id: events.append(('START', run_id)))
    comp.on_stop(lambda run_id: events.append(('STOP', run_id)))

    print(f'\nreal competition.py against {args.broker}:{args.port}\n')
    comp.connect()
    check('library connected and subscribed', comp._client is not None)
    check('MAC formatted as the server stores it', comp.mac == MAC, comp.mac)

    srv = publisher(args)
    robot_topic = f'robosteam/robot/{MAC}/cmd'
    broadcast = f'robosteam/competition/{COMPETITION_ID}/cmd'
    status_topic = f'robosteam/robot/{MAC}/status'

    # Watch what the library publishes back.
    replies = []
    srv.on_message = lambda _c, _u, m: replies.append(json.loads(m.payload.decode()))
    srv.subscribe(status_topic, qos=1)
    time.sleep(1)

    # ── unbound robot obeys the broadcast ────────────────────────────────────
    srv.publish(broadcast, json.dumps({'cmd': 'START', 'run_id': 101}), qos=1)
    pump(comp)
    check('unbound robot starts on the broadcast', events == [('START', 101)], events)
    check('comp.running is True', comp.running is True)
    check('not yet flagged as bound', comp.bound_to_team is False)

    srv.publish(broadcast, json.dumps({'cmd': 'STOP'}), qos=1)
    pump(comp)
    events.clear()

    # ── a targeted command binds it ──────────────────────────────────────────
    srv.publish(robot_topic, json.dumps({'cmd': 'START', 'run_id': 202}), qos=1)
    pump(comp)
    check('targeted START fires the callback', events == [('START', 202)], events)
    check('bound_to_team latched', comp.bound_to_team is True)

    srv.publish(robot_topic,
                json.dumps({'cmd': 'STOP', 'run_id': 202, 'reason': 'finished'}), qos=1)
    pump(comp)
    check("stop_reason is 'finished'", comp.stop_reason == 'finished', comp.stop_reason)
    events.clear()

    # ── THE ONE THAT MATTERS ─────────────────────────────────────────────────
    srv.publish(broadcast, json.dumps({'cmd': 'START', 'run_id': 303}), qos=1)
    pump(comp)
    check("BOUND ROBOT IGNORES ANOTHER TEAM'S BROADCAST START", events == [], events)
    check('still stopped', comp.running is False)

    # ── PING / PONG over the real broker ─────────────────────────────────────
    replies.clear()
    nonce = 'live-check-nonce-01'
    sent = time.monotonic()
    srv.publish(robot_topic, json.dumps({'cmd': 'PING', 'nonce': nonce}), qos=1)
    pong = None
    while time.monotonic() - sent < 8 and pong is None:
        comp.poll()
        time.sleep(0.02)
        pong = next((r for r in replies if r.get('cmd') == 'PONG'), None)
    rtt = int((time.monotonic() - sent) * 1000)

    check('PONG came back over the real broker', pong is not None)
    if pong:
        check('nonce echoed unchanged', pong.get('nonce') == nonce, pong)
        check('firmware version reported', pong.get('firmware') == FIRMWARE_VERSION, pong)
        print(f'       round trip {rtt} ms')

    # ── duplicate delivery, as a bound robot really sees it ──────────────────
    events.clear()
    srv.publish(robot_topic, json.dumps({'cmd': 'START', 'run_id': 404}), qos=1)
    srv.publish(broadcast, json.dumps({'cmd': 'START', 'run_id': 404}), qos=1)
    pump(comp)
    check('on_start fires once, not twice', events == [('START', 404)], events)

    comp._client.disconnect()
    srv.loop_stop()
    srv.disconnect()

    print(f'\n{checks - len(failures)}/{checks} checks passed')
    if failures:
        print('FAILED: ' + ', '.join(failures))
        return 1
    print('PICOBOT LIVE BROKER CHECK PASSED')
    return 0


if __name__ == '__main__':
    sys.exit(main())
