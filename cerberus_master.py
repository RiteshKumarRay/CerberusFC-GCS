import serial
import time
import struct
import socket
import select
import sys
import json

# --- CONFIGURATION ---
FC_PORT = '/dev/ttyACM0'
LORA_PORT = '/dev/serial0'
BAUD_RATE = 115200

UDP_IP = "127.0.0.1"
UDP_PORT_IN = 5005
UDP_PORT_OUT = 5006

# --- NETWORK SETUP ---
sock_in = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock_in.bind((UDP_IP, UDP_PORT_IN))
sock_in.setblocking(0)

sock_out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

# --- SERIAL SETUP ---
try:
    fc = serial.Serial(FC_PORT, BAUD_RATE, timeout=0.05)
except Exception as e:
    print(f"❌ FATAL ERROR: Cannot open FC port {FC_PORT}. {e}")
    sys.exit(1)

try:
    lora = serial.Serial(LORA_PORT, BAUD_RATE, timeout=0.1)
    lora_active = True
except:
    print("⚠️ LoRa UART not detected. Continuing without external telemetry.")
    lora_active = False

# --- MSP COMMANDS ---
MSP_ATTITUDE = 108
MSP_RAW_GPS  = 106
MSP_ANALOG   = 110
MSP_STATUS   = 101
MSP_ALTITUDE = 109

def send_msp(cmd, payload=b''):
    size = len(payload)
    checksum = size ^ cmd
    for b in payload:
        checksum ^= b
    packet = b'$M<' + struct.pack('<BB', size, cmd) + payload + struct.pack('<B', checksum)
    fc.write(packet)

def request_and_read(cmd_id):
    fc.reset_input_buffer()
    send_msp(cmd_id)
    for _ in range(30):
        header = fc.read(3)
        if len(header) < 3: return None, None
        if header == b'$M>':
            sc = fc.read(2)
            if len(sc) < 2: return None, None
            size, cmd = struct.unpack('<BB', sc)
            payload = fc.read(size)
            fc.read(1)  # checksum
            return cmd, payload
    return None, None

def send_raw_rc(channels):
    # Dynamically handles 8 channels and pads the rest to 16
    padded_channels = channels + [1500] * (16 - len(channels))
    payload = struct.pack('<' + 'H' * 16, *padded_channels)
    send_msp(200, payload)

def upload_mission(waypoints):
    print(f"\n🚀 Injecting {len(waypoints)} Waypoints...")
    action_map = {'waypoint': 1, 'hover': 3, 'rth': 4, 'land': 11, 'loiter': 3}

    for index, wp in enumerate(waypoints):
        lat_msp = int(float(wp['lat']) * 10000000)
        lon_msp = int(float(wp['lon']) * 10000000)
        alt_cm = int(wp.get('altitude', 20) * 100)
        action_code = action_map.get(wp.get('action', 'waypoint'), 1)
        p1 = int(wp.get('loiter_time', 0)) if action_code == 3 else 0
        flag = 165 if index == (len(waypoints) - 1) else 0

        wp_payload = struct.pack('<BBiiihhhB', index + 1, action_code, lat_msp, lon_msp, alt_cm, p1, 0, 0, flag)
        send_msp(209, wp_payload)

        if flag == 165:
            time.sleep(0.5)
            send_msp(250)  # EEPROM_WRITE
            print("✅ Mission Flashed to FC.")

# --- GLOBAL STATE ---
SYSTEM_STATE = "DISARMED"
state_timer = 0

# Channel map: Roll, Pitch, Throttle, Yaw, Aux1, Aux2(CH6), CH7, CH8
# CH7 (index 6): 1000 = Disarmed, 1800 = Armed
rc_channels = [1500, 1500, 1000, 1500, 1000, 1000, 1000, 1500]

last_rc_time = 0
last_tel_time = 0

t_data = {
    "type": "telemetry",
    "attitude": {"roll": 0.0, "pitch": 0.0, "yaw": 0},
    "gps": {"fix": False, "sats": 0, "lat": 0.0, "lon": 0.0, "speed": 0.0},
    "altitude": 0.0, "vario_ms": 0.0,
    "battery": {"voltage": 0.0, "current": 0.0, "mah": 0},
    "system": {"armed": False},
    "mode": "IDLE"
}

print("🛸 Cerberus Master Daemon Active. Handling FC, LoRa, and Dashboard...\n")

while True:
    current_time = time.time()

    # 1. READ COMMANDS FROM NODE-RED
    ready = select.select([sock_in], [], [], 0.001)
    if ready[0]:
        data, addr = sock_in.recvfrom(2048)
        try:
            raw_json = json.loads(data.decode('utf-8').strip())
            payload = raw_json.get('payload', raw_json) if isinstance(raw_json, dict) else raw_json
            cmd = payload.get('command')

            if cmd == 'arm' and SYSTEM_STATE == "DISARMED":
                print("\n🚀 Executing Arming Sequence...")
                SYSTEM_STATE = "BYPASSING_CHECKS"
                state_timer = current_time
            elif cmd == 'launch' and SYSTEM_STATE == "ARMED_IDLE":
                print("\n🗺️ Dashboard Countdown Complete. Engaging Mission Mode!")
                SYSTEM_STATE = "MISSION_ACTIVE"
            elif cmd == 'disarm':
                print("\n🛑 EMERGENCY DISARM!")
                SYSTEM_STATE = "DISARMED"
                rc_channels = [1500, 1500, 1000, 1500, 1000, 1000, 1000, 1500]
            elif cmd == 'upload':
                upload_mission(payload.get('waypoints', []))
            elif cmd == 'clear':
                send_msp(209, struct.pack('<BBiiihhhB', 1, 1, 0, 0, 0, 0, 0, 0, 165))
                time.sleep(0.5)
                send_msp(250)
                print("\n🗑️ Mission Cleared.")
        except Exception as e:
            pass  # Silent drop

    # 2. STATE MACHINE
    if SYSTEM_STATE == "BYPASSING_CHECKS":
        # Hold arm gesture: Throttle Low, Yaw Right, CH7 Armed (1800)
        rc_channels = [1500, 1500, 1000, 2000, 1000, 1000, 1800, 1500]

        # KEY FIX: Transition the INSTANT telemetry confirms arming.
        # This mirrors a pilot snapping yaw to center the moment the FC accepts
        # the gesture — instead of blindly holding Yaw=2000 for a fixed 2 seconds
        # (which causes fast spin post-arm because INAV sees yaw deflection while armed).
        if t_data["system"]["armed"]:
            print("\n✅ FC confirmed armed. Centering yaw immediately.")
            SYSTEM_STATE = "PRE_ARM_CENTER"
            state_timer = current_time
        elif current_time - state_timer > 5.0:
            # Safety: abort if FC doesn't arm within 5 seconds
            print("\n⚠️ Arming timeout! FC did not arm within 5s. Check INAV config. Aborting.")
            SYSTEM_STATE = "DISARMED"
            rc_channels = [1500, 1500, 1000, 1500, 1000, 1000, 1000, 1500]

    elif SYSTEM_STATE == "PRE_ARM_CENTER":
        # Yaw back to center (1500), CH7 stays Armed (1800). Hold 0.5s to settle.
        rc_channels = [1500, 1500, 1000, 1500, 1000, 1000, 1800, 1500]
        if current_time - state_timer > 0.5:
            SYSTEM_STATE = "ARMED_IDLE"
            print("\n✅ Armed! Motors at idle. Waiting for Launch command from dashboard...")

    elif SYSTEM_STATE == "ARMED_IDLE":
        # Drone sits here safely FOREVER until dashboard sends 'launch'.
        # Throttle LOW (1000), all sticks centered, CH7 Armed (1800).
        rc_channels = [1500, 1500, 1000, 1500, 1000, 1000, 1800, 1500]

    elif SYSTEM_STATE == "MISSION_ACTIVE":
        # Throttle Mid (1500), CH6 WP Nav Triggered (1800), CH7 Armed (1800)
        rc_channels = [1500, 1500, 1500, 1500, 1000, 1800, 1800, 1500]

    elif SYSTEM_STATE == "DISARMED":
        # Everything safe
        rc_channels = [1500, 1500, 1000, 1500, 1000, 1000, 1000, 1500]

    # 3. WRITE RC DATA (50Hz)
    if current_time - last_rc_time >= 0.02:
        send_raw_rc(rc_channels)
        last_rc_time = current_time

    # 4. POLL & PUBLISH TELEMETRY (10Hz)
    if current_time - last_tel_time >= 0.10:
        last_tel_time = current_time

        # Attitude
        c, p = request_and_read(MSP_ATTITUDE)
        if c == MSP_ATTITUDE and p and len(p) >= 6:
            r, pt, y = struct.unpack('<hhh', p[:6])
            t_data["attitude"] = {"roll": round(r/10.0, 1), "pitch": round(pt/10.0, 1), "yaw": y}

        # GPS
        c, p = request_and_read(MSP_RAW_GPS)
        if c == MSP_RAW_GPS and p and len(p) >= 16:
            f, s, lat, lon, alt, spd, _ = struct.unpack('<BBiiHHH', p[:16])
            t_data["gps"] = {"fix": f > 0, "sats": s, "lat": round(lat/10000000.0, 7), "lon": round(lon/10000000.0, 7), "speed": round(spd/100.0, 1)}

        # Altitude
        c, p = request_and_read(MSP_ALTITUDE)
        if c == MSP_ALTITUDE and p and len(p) >= 6:
            ea, va = struct.unpack('<ih', p[:6])
            t_data["altitude"] = round(ea/100.0, 1)
            t_data["vario_ms"] = round(va/100.0, 1)

        # Battery
        c, p = request_and_read(MSP_ANALOG)
        if c == MSP_ANALOG and p and len(p) >= 7:
            vb, m, rs, am = struct.unpack('<BHHH', p[:7])
            t_data["battery"] = {"voltage": round(vb/10.0, 1), "mah": m, "current": round(am/100.0, 2)}

        # Status
        c, p = request_and_read(MSP_STATUS)
        if c == MSP_STATUS and p and len(p) >= 11:
            _, _, _, flag, _ = struct.unpack('<HHHIB', p[:11])
            is_armed = bool(flag & 1)
            t_data["system"]["armed"] = is_armed

            # Smart Mode Labeling
            if SYSTEM_STATE == "MISSION_ACTIVE":  t_data["mode"] = "WP MISSION"
            elif SYSTEM_STATE == "ARMED_IDLE":    t_data["mode"] = "ARMED IDLE"
            elif SYSTEM_STATE == "DISARMED":      t_data["mode"] = "IDLE"
            elif is_armed:                        t_data["mode"] = "ARMED"

        # Push to Node-RED via UDP
        sock_out.sendto(json.dumps(t_data).encode('utf-8'), (UDP_IP, UDP_PORT_OUT))

        # Terminal Heartbeat — shows current state so you can watch the transition
        sys.stdout.write(f"📡 [{SYSTEM_STATE}] Armed: {t_data['system']['armed']} | Alt: {t_data['altitude']}m      \r")
        sys.stdout.flush()

        # Push to LoRa via UART
        if lora_active:
            try:
                arm_flag = 1 if t_data["system"]["armed"] else 0
                fix_flag = 1 if t_data["gps"]["fix"] else 0
                csv = f"T,{arm_flag},{t_data['attitude']['roll']},{t_data['attitude']['pitch']},{t_data['attitude']['yaw']},{t_data['altitude']},{t_data['vario_ms']},{fix_flag},{t_data['gps']['sats']},{t_data['gps']['lat']},{t_data['gps']['lon']},{t_data['gps']['speed']},{t_data['battery']['voltage']},{t_data['battery']['current']},{t_data['battery']['mah']}\n"
                lora.write(csv.encode('utf-8'))
            except:
                pass
