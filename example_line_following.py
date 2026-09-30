"""
RoboSTEAM Academy -- Line Following Competition Example

This file shows students how to integrate the Competition library with
their own PicoBot line-following code.

Copy credentials.py.example -> credentials.py and fill in real values.
Upload competition.py and credentials.py to the Pico alongside this file.
"""

import time
import credentials as c
from competition import Competition

# ── Hardware setup (adapt to your PicoBot wiring) ────────────
# Replace this section with your actual motor / sensor imports.
# Example uses placeholder functions -- fill in real code.

def motors_forward(speed=50):
    """Drive forward at speed (0-100)."""
    pass  # TODO: robot.drive(speed, speed)

def motors_stop():
    """Hard stop all motors."""
    pass  # TODO: robot.stop()

def read_line_sensors():
    """Return (left, right) sensor values (True = on line)."""
    return (False, False)  # TODO: read real IR sensors

# ── Competition integration ───────────────────────────────────

comp = Competition(
    ssid=c.WIFI_SSID,
    password=c.WIFI_PASSWORD,
    broker=c.MQTT_BROKER,
    port=c.MQTT_PORT,
    mqtt_user=c.MQTT_USER,
    mqtt_password=c.MQTT_PASSWORD,
    competition_id=c.COMPETITION_ID,
    friendly_name="Team Robotics Alpha",  # shown in judge UI
)


def on_start(run_id):
    """Called immediately when the server sends START."""
    print("Run", run_id, "started -- enabling drive")
    motors_forward(40)


def on_stop(run_id):
    """Called immediately when the server sends STOP."""
    print("Run", run_id, "stopped -- halting motors")
    motors_stop()


comp.on_start(on_start)
comp.on_stop(on_stop)
comp.connect()   # blocks until WiFi + MQTT are ready

print("Waiting for START from judge...")

# ── Main loop ─────────────────────────────────────────────────
while True:
    comp.poll()   # must be called often -- handles MQTT + heartbeat

    if comp.running:
        # PID line-following logic runs here between START and STOP.
        left, right = read_line_sensors()

        if left and not right:
            # Drifted left -- steer right
            pass  # TODO: robot.steer_right()
        elif right and not left:
            # Drifted right -- steer left
            pass  # TODO: robot.steer_left()
        else:
            motors_forward(50)

    time.sleep_ms(20)
