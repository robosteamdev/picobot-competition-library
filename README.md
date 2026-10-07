# picobot-competition-library

![Co-funded by the European Union · ROBO STEAM ACADEMY · partner logos](images/logos_strip.png)

**Erasmus+ project ROBO STEAM ACADEMY** (KA220-VET-7CF4F308) — co-funded by the European Union — <https://robosteam.eu/>

Part of the **PicoBot Teachers' Toolkit** (lesson plans, student materials, slides and guides in five languages):
<https://github.com/robosteamdev/robo-steam-academy-teachers-toolkit>

MicroPython library for Raspberry Pi Pico W robots participating in
RoboSTEAM Academy competitions.

The library handles WiFi connection, MQTT communication, START/STOP command
reception, connection tests and heartbeat publishing. Students write only their
autonomous robot logic.

**Before your first competition:** claim your robot on the website using the MAC
address it prints on startup, and bind it to your team. Until you do, it will
start whenever *any* team in your category is sent off. See
[Claiming your robot](#claiming-your-robot-and-why-it-matters).

---

## Files to upload to your Pico

| File | Purpose |
|------|---------|
| `competition.py` | Competition client library |
| `credentials.py` | Your WiFi + MQTT credentials (gitignored) |
| Your own code | Line-following, arm, etc. |

---

## Quick start

**1. Copy and fill in credentials:**

```
cp credentials.py.example credentials.py
# Edit credentials.py with your WiFi SSID/password and MQTT settings
```

**2. Upload to the Pico** (using Thonny or mpremote):

```
mpremote cp competition.py :competition.py
mpremote cp credentials.py :credentials.py
mpremote cp my_robot.py :main.py
```

**3. Write your robot code:**

```python
import time
import credentials as c
from competition import Competition

comp = Competition(
    ssid=c.WIFI_SSID, password=c.WIFI_PASSWORD,
    broker=c.MQTT_BROKER, port=c.MQTT_PORT,
    mqtt_user=c.MQTT_USER, mqtt_password=c.MQTT_PASSWORD,
    competition_id=c.COMPETITION_ID,
    friendly_name="My Team Robot",
)

def on_start(run_id):
    motors.forward(50)

def on_stop(run_id):
    motors.stop()

comp.on_start(on_start)
comp.on_stop(on_stop)
comp.connect()

while True:
    comp.poll()          # call every 20-50 ms -- handles MQTT
    if comp.running:
        line_follow()    # your code here
    time.sleep_ms(20)
```

See `example_line_following.py` for a complete template.

---

## Testing without a Pico

| Script | What it proves | Needs |
|--------|----------------|-------|
| `test_competition.py` | The decision logic — topic routing, the broadcast latch, deduplication, the PONG contract. Fake MQTT client | Nothing. `python test_competition.py` |
| `live_broker_check.py` | The **same library against a real broker**: real subscriptions, real messages, real PING round trip. Only WiFi and the MicroPython tick helpers are stubbed | Python 3 with `paho-mqtt` on a computer that can reach the broker |

```bash
python3 live_broker_check.py --broker <broker-address> --port 1883 \
    --user <mqtt-user> --password <mqtt-password>
```

The second one exists so that a bench session debugs WiFi, GPIO and timing
rather than discovering a protocol mistake with a soldering iron in hand.

---

## API reference

### `Competition(...)`

Constructor parameters:

| Parameter | Type | Description |
|-----------|------|-------------|
| `ssid` | str | WiFi network name |
| `password` | str | WiFi password |
| `broker` | str | MQTT broker address (IP or host name) |
| `port` | int | MQTT port (default `1883`) |
| `mqtt_user` | str | MQTT username |
| `mqtt_password` | str | MQTT password |
| `competition_id` | int | Category DB id from the server |
| `friendly_name` | str | Display name shown in judge UI |

### `comp.on_start(callback)`

Register a function to call when the judge starts your run.
`callback(run_id)` — `run_id` is an integer.

### `comp.on_stop(callback)`

Register a function to call when the judge stops your run.
`callback(run_id)` — `run_id` is an integer.

### `comp.connect()`

Connect WiFi and MQTT. Blocks until both are ready.
Raises `RuntimeError` if WiFi times out after 20 seconds.

### `comp.poll()`

Non-blocking check for incoming MQTT messages.
**Call this every 20-50 ms** from your main loop.
Handles automatic reconnection if the broker drops.

### `comp.run()`

Alternative to a manual loop: connects and polls forever in a 20 ms loop.
Use when all logic is inside `on_start` / `on_stop` callbacks.

### `comp.running` (property)

`True` between START and STOP. Check this in your main loop.

### `comp.run_id` (property)

Integer run ID from the last START, or `None`.

### `comp.mac` (property)

MAC address as `"AA:BB:CC:DD:EE:FF"`. Available after `connect()`, and printed
to the serial console on connect. **This is what you type on the website to
claim your robot** — see "Claiming your robot" below.

### `comp.stop_reason` (property)

Why the last run ended: `"finished"` (the lap timer saw the final crossing),
`"timeout"` (the run ran out of time), `"disconnected"` (the robot lost its
connection to the competition server during the run), or `None` (a judge stopped
it by hand). Handy for a status LED. Cleared on the next START.

### `comp.bound_to_team` (property)

`True` once the server has addressed this robot personally, which happens only
after a team has bound it on the website. See the next section.

---

## Claiming your robot, and why it matters

1. Power up the robot and read the **MAC address** from the serial console:
   `[Competition] MAC: AA:BB:CC:DD:EE:FF`
2. On the website, go to **My Robots**, type that MAC and claim it.
3. Rename it to something you will recognise, and bind it to your team for the
   competition you have joined.
4. Press **Test connection** — you should get a round-trip time in milliseconds.
   That is a real `PING` to this library and a `PONG` back.

**Until step 3, the robot obeys the category broadcast** — meaning it starts
whenever *any* team in your category is sent off. Once bound, the server
commands it personally and the library ignores the broadcast. `comp.bound_to_team`
tells you which mode you are in.

This is not optional politeness: at a real event, an unbound robot drives off
during someone else's run.

---

## MQTT topics (for reference)

| Topic | Direction | Purpose |
|-------|-----------|---------|
| `robosteam/robot/{mac}/cmd` | Server → this robot | **START/STOP for your run, and PING.** The real one |
| `robosteam/competition/{id}/cmd` | Server → all robots in the category | Broadcast START/STOP. Obeyed only until the robot is bound |
| `robosteam/robot/{mac}/status` | Robot → Server | Heartbeat every 30 s, and PONG replies |

Payloads:

```json
{"cmd": "START", "run_id": 42}
{"cmd": "STOP",  "run_id": 42, "reason": "finished"}
{"cmd": "PING",  "nonce": "9c302b032ef6458c"}
```

The library answers a `PING` automatically with:

```json
{"device_id": "AA:BB:CC:DD:EE:FF", "cmd": "PONG",
 "nonce": "9c302b032ef6458c", "firmware": "1.1.1"}
```

The nonce must be echoed back **unchanged** — the server drops a reply it does
not recognise, so a mangled nonce reads as "no answer". You do not have to do
anything for this; it is handled inside `poll()`.

Full protocol: `docs/MQTT_PROTOCOL.md` in <https://github.com/robosteamdev/robosteam-competitions>.

---

## Important notes

- **Never report timing** — the lap timer hardware is the clock, not the robot.
- **A lost connection ends the run.** If the connection to the competition server
  breaks during a run, the robot can no longer hear STOP. The library then sets
  `running` to `False` and calls your `on_stop` callback itself (once), with
  `stop_reason == "disconnected"`. The motor driver keeps its last command, so
  stop the motors in `on_stop` — and, to be safe, whenever `comp.running` is
  `False` in your main loop. A STOP for the same run that arrives after the
  reconnect is ignored as a duplicate.
- **Claim and bind your robot before the event** (see above), or it will start
  during other teams' runs.
- `credentials.py` is gitignored. Never commit it.
- The `friendly_name` appears in the judge UI. Use your team name.
- If your `on_start` callback runs a blocking loop, `comp.poll()` won't fire
  during that time — so `PING` goes unanswered and the robot looks offline.
  Keep callbacks short and do the work in your main loop under `comp.running`.
- **Power-cycling clears `bound_to_team`** until the next command arrives. The
  robot falls back to obeying the broadcast in the meantime, which is the safe
  direction: it responds to too much rather than to nothing.

---

## In the toolkit and the competitions

- Module **M12** Capstone of the PicoBot Teachers' Toolkit: session 25 (competitions: START/STOP from the platform)
  and the capstone runs.
- Competition platform: <https://competitions.robosteam.eu/> — its code: <https://github.com/robosteamdev/robosteam-competitions>
- The lap timer (the clock of every run): <https://github.com/robosteamdev/robosteam-lap-timer>

## Licence and credits

Code: MIT licence (see `LICENSE`). Please credit "ROBO STEAM ACADEMY, Erasmus+ project KA220-VET-7CF4F308" —
<https://robosteam.eu/>

Funded by the European Union. Views and opinions expressed are however those of the author(s) only and do not
necessarily reflect those of the European Union or the Human Resource Development Centre (HRDC). Neither the European
Union nor the granting authority can be held responsible for them.
