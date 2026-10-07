"""
RoboSTEAM Academy -- PicoBot Competition Library
Pico W MicroPython library for competition-aware robots.

The competition server (not the robot) controls timing. The robot receives
START and STOP commands over MQTT and enables/disables student movement code.
The lap timer hardware is the authoritative clock -- this library never
reports timing.

Two topics carry commands, and the difference matters:

  robosteam/robot/{mac}/cmd          <- YOUR robot only. This is the real one.
  robosteam/competition/{id}/cmd     <- every robot in the category at once.

The server sends both. Once this robot has been claimed by a team and bound to
it on the website, the server addresses it personally, and from that moment the
library ignores the broadcast -- otherwise your robot would also start when a
DIFFERENT team is sent off. A robot that has never been addressed personally
(not yet bound, or an older server) keeps obeying the broadcast so it still
works. See docs/MQTT_PROTOCOL.md.

Copy credentials.py.example -> credentials.py and fill in real values before
uploading to the Pico.
"""

import json
import network
import time
import ubinascii
from umqtt.simple import MQTTClient

#: Reported to the server in every heartbeat and PONG, and shown in the
#: organiser device console. Bump it when you change this file.
FIRMWARE_VERSION = "1.1.1"

_HEARTBEAT_MS = 30_000
_RECONNECT_WAIT_MS = 5_000
_WIFI_TIMEOUT_MS = 20_000


class Competition:
    """
    Student-facing competition client.

    Basic usage::

        from competition import Competition
        import credentials as c

        comp = Competition(
            ssid=c.WIFI_SSID, password=c.WIFI_PASSWORD,
            broker=c.MQTT_BROKER, port=c.MQTT_PORT,
            mqtt_user=c.MQTT_USER, mqtt_password=c.MQTT_PASSWORD,
            competition_id=c.COMPETITION_ID,
        )

        def handle_start(run_id):
            motors.forward(50)

        def handle_stop(run_id):
            motors.stop()

        comp.on_start(handle_start)
        comp.on_stop(handle_stop)
        comp.connect()

        while True:
            comp.poll()          # call frequently -- handles MQTT + heartbeat
            if comp.running:
                line_follow()    # student autonomous code runs here
    """

    def __init__(self, ssid, password, broker, port,
                 mqtt_user, mqtt_password, competition_id,
                 friendly_name="PicoBot"):
        self._ssid = ssid
        self._password = password
        self._broker = broker
        self._port = port
        self._mqtt_user = mqtt_user
        self._mqtt_password = mqtt_password
        self._competition_id = competition_id
        self._friendly_name = friendly_name

        self._start_cb = None
        self._stop_cb = None
        self._running = False
        self._current_run_id = None
        self._stop_reason = None
        self._client = None
        self._mac = None
        self._topic_cmd_robot = None
        self._topic_cmd_competition = None
        self._topic_status = None
        self._last_heartbeat_ms = 0
        self._last_reconnect_ms = -_RECONNECT_WAIT_MS  # allow immediate first connect

        # True once the server has addressed this robot on its own topic, which
        # only happens when a team has bound it. From then on the broadcast is
        # someone else's business. Latched, not per-message: the broadcast for
        # another team's run carries no clue that it is not for us.
        self._addressed_directly = False

        # (cmd, run_id) of the last command acted on. A bound robot receives
        # START on both topics within milliseconds of each other, and firing
        # on_start twice would restart the student's code mid-run.
        self._last_command = None

    # ── Public API ──────────────────────────────────────────────

    @property
    def running(self):
        """True when the server has sent START and not yet STOP."""
        return self._running

    @property
    def run_id(self):
        """Current run_id from the last START command, or None."""
        return self._current_run_id

    @property
    def mac(self):
        """Device MAC address as 'AA:BB:CC:DD:EE:FF'. Available after connect().

        This is what a student types on the website to claim the robot, so it is
        printed on connect.
        """
        return self._mac

    @property
    def stop_reason(self):
        """Why the last run ended: 'finished', 'timeout', 'disconnected', or
        None if a judge voided it by hand. Useful for a status LED -- 'finished'
        means the lap timer saw the final crossing, 'timeout' means the run ran
        out of time, 'disconnected' means the robot lost its connection to the
        competition server during the run (on_stop was called by the library).
        """
        return self._stop_reason

    @property
    def bound_to_team(self):
        """True once the server has addressed this robot on its own topic.

        False means either nobody has claimed and bound it on the website yet,
        or it has been power-cycled since the last command. While it is False
        the robot still obeys the competition broadcast.
        """
        return self._addressed_directly

    def on_start(self, callback):
        """Register callback(run_id) to be called when the server sends START."""
        self._start_cb = callback

    def on_stop(self, callback):
        """Register callback(run_id) to be called when the server sends STOP."""
        self._stop_cb = callback

    def connect(self):
        """
        Connect WiFi and MQTT. Blocks until both are up.
        Raises RuntimeError if WiFi times out.
        """
        self._wifi_connect()
        self._build_topics()
        self._mqtt_connect()

    def poll(self):
        """
        Non-blocking check for MQTT messages and heartbeat.
        Call this every 20-50 ms from your main loop.
        Reconnects automatically if the broker drops.
        """
        if self._client is None:
            now = time.ticks_ms()
            if time.ticks_diff(now, self._last_reconnect_ms) >= _RECONNECT_WAIT_MS:
                self._last_reconnect_ms = now
                self._mqtt_connect()
            return
        try:
            self._client.check_msg()
            now = time.ticks_ms()
            if time.ticks_diff(now, self._last_heartbeat_ms) >= _HEARTBEAT_MS:
                self._publish_heartbeat()
        except Exception as exc:
            print("[Competition] MQTT error:", repr(exc))
            self._client = None
            self._connection_lost()   # safety: stop the run on disconnect

    def run(self):
        """
        Block forever: connect then poll in a 20 ms loop.
        Use this for robots whose entire logic lives in on_start/on_stop
        callbacks. If your robot needs a custom main loop, call connect()
        then poll() manually instead.
        """
        self.connect()
        while True:
            self.poll()
            time.sleep_ms(20)

    # ── Private ─────────────────────────────────────────────────

    def _connection_lost(self):
        """The broker connection broke. If a run was active, end it here.

        Without a connection the robot can no longer hear STOP, so it must not
        keep driving. Clearing `running` alone is not enough: the motor driver
        keeps its last command, and a program that stops its motors only in
        on_stop() would drive on. So the library calls on_stop() itself, once,
        with stop_reason 'disconnected'. A STOP for the same run that arrives
        after reconnecting is then recognised as a duplicate and ignored.
        """
        was_running = self._running
        self._running = False
        if not was_running:
            return
        run_id = self._current_run_id
        self._stop_reason = "disconnected"
        self._last_command = ("STOP", run_id)
        print("[Competition] Connection lost during run_id=%s -- stopping" % run_id)
        if self._stop_cb:
            try:
                self._stop_cb(run_id)
            except Exception as exc:
                print("[Competition] on_stop error:", repr(exc))

    def _wifi_connect(self):
        wlan = network.WLAN(network.STA_IF)
        wlan.active(True)
        if not wlan.isconnected():
            print("[Competition] Connecting WiFi:", self._ssid)
            wlan.connect(self._ssid, self._password)
            deadline = time.ticks_add(time.ticks_ms(), _WIFI_TIMEOUT_MS)
            while not wlan.isconnected():
                if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                    raise RuntimeError("WiFi connect timeout -- check SSID/password")
                time.sleep_ms(250)
                print(".", end="")
            print()
        mac_bytes = wlan.config("mac")
        self._mac = ubinascii.hexlify(mac_bytes, ":").decode().upper()
        print("[Competition] WiFi ready. IP:", wlan.ifconfig()[0])
        print("[Competition] MAC:", self._mac)

    def _build_topics(self):
        self._topic_cmd_robot = "robosteam/robot/%s/cmd" % self._mac
        self._topic_cmd_competition = "robosteam/competition/%d/cmd" % self._competition_id
        self._topic_status = "robosteam/robot/%s/status" % self._mac

    def _mqtt_connect(self):
        try:
            client = MQTTClient(
                client_id=self._mac,
                server=self._broker,
                port=self._port,
                user=self._mqtt_user,
                password=self._mqtt_password,
                keepalive=60,
            )
            client.set_callback(self._on_message)
            client.connect()
            client.subscribe(self._topic_cmd_robot)
            client.subscribe(self._topic_cmd_competition)
            self._client = client
            self._last_heartbeat_ms = time.ticks_ms()
            self._publish_heartbeat()
            print("[Competition] MQTT connected.")
            print("  robot cmd  :", self._topic_cmd_robot)
            print("  contest cmd:", self._topic_cmd_competition)
            print("  status     :", self._topic_status)
        except Exception as exc:
            print("[Competition] MQTT connect failed:", repr(exc))
            self._client = None

    def _on_message(self, topic, payload):
        try:
            data = json.loads(payload)
        except Exception:
            print("[Competition] Bad JSON:", payload)
            return

        # umqtt hands back bytes; the topic decides whether this command is ours.
        if isinstance(topic, bytes):
            topic = topic.decode()
        personal = (topic == self._topic_cmd_robot)

        cmd = data.get("cmd", "")

        if cmd == "PING":
            # Answer regardless of topic: a connection test is always addressed
            # to this robot, and it must work before any team has bound it.
            self._publish_pong(data.get("nonce"))
            return

        if personal:
            # The server knows who we are. Stop listening to the broadcast.
            if not self._addressed_directly:
                self._addressed_directly = True
                print("[Competition] Bound to a team -- ignoring the broadcast "
                      "topic from now on.")
        elif self._addressed_directly:
            # A broadcast for somebody else's run. Before Phase 5.21 this line
            # did not exist and every robot in the category started at once.
            print("[Competition] Broadcast %s ignored (not addressed to us)" % cmd)
            return

        run_id = data.get("run_id")

        # A bound robot gets each command on both topics within milliseconds.
        # A broadcast STOP carries no run_id, so fall back to the run we think
        # we are in, or the two spellings would not compare equal.
        key = (cmd, run_id if run_id is not None else self._current_run_id)
        if key == self._last_command:
            return
        self._last_command = key

        if cmd == "START":
            self._running = True
            self._current_run_id = run_id
            self._stop_reason = None
            print("[Competition] START run_id=%s" % run_id)
            if self._start_cb:
                try:
                    self._start_cb(run_id)
                except Exception as exc:
                    print("[Competition] on_start error:", repr(exc))
        elif cmd == "STOP":
            self._running = False
            self._stop_reason = data.get("reason")
            print("[Competition] STOP  run_id=%s reason=%s"
                  % (run_id, self._stop_reason))
            if self._stop_cb:
                try:
                    self._stop_cb(run_id)
                except Exception as exc:
                    print("[Competition] on_stop error:", repr(exc))
        else:
            print("[Competition] Unknown cmd ignored:", cmd)

    def _publish_pong(self, nonce):
        """Answer a connection test, echoing the nonce unchanged.

        The reply rides on the status topic, so it doubles as a heartbeat. The
        server matches the nonce to the pending test and shows the round-trip
        time on the student's My Robots page; a nonce it does not recognise is
        dropped, so echoing it exactly is the whole contract.
        """
        if self._client is None:
            return
        payload = json.dumps({
            "device_id": self._mac,
            "cmd": "PONG",
            "nonce": nonce,
            "firmware": FIRMWARE_VERSION,
            "friendly_name": self._friendly_name,
        })
        try:
            self._client.publish(self._topic_status, payload)
            self._last_heartbeat_ms = time.ticks_ms()
            print("[Competition] PING -> PONG (nonce=%s)" % nonce)
        except Exception as exc:
            print("[Competition] PONG failed:", repr(exc))

    def _publish_heartbeat(self):
        if self._client is None:
            return
        payload = json.dumps({
            "device_id": self._mac,
            "friendly_name": self._friendly_name,
            "firmware": FIRMWARE_VERSION,
            "competition_id": self._competition_id,
            "running": self._running,
            "run_id": self._current_run_id,
        })
        try:
            self._client.publish(self._topic_status, payload)
            self._last_heartbeat_ms = time.ticks_ms()
        except Exception as exc:
            print("[Competition] Heartbeat failed:", repr(exc))
