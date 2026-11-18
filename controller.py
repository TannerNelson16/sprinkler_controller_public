import gc
try:
    gc.threshold(4096)
except Exception:
    pass
gc.collect()  # Collect immediately to start with a clean heap

FIRMWARE_VERSION = "2.1.3"

import network
from microdot_asyncio import Microdot, send_file
gc.collect()
import machine
from machine import Pin, RTC, reset
import ujson
import utime as time
import uasyncio as asyncio
import usocket
import uos as os

try:
    from cloud import CloudClient
except Exception:
    CloudClient = None
gc.collect()

# Drop precompressed HTML left behind by an older firmware so stale pages are never served.
try:
    _gz_ver = ''
    try:
        with open('.gz_ver', 'r') as _f:
            _gz_ver = _f.read().strip()
    except Exception:
        pass
    if _gz_ver != FIRMWARE_VERSION:
        for _n in ('index.html.gz', 'logs.html.gz', 'scheduler.html.gz', 'settings.html.gz'):
            try:
                os.remove(_n)
            except Exception:
                pass
        with open('.gz_ver', 'w') as _f:
            _f.write(FIRMWARE_VERSION)
except Exception:
    pass
gc.collect()

global MQTT

timers = {}
run_queue = []
current_queue_index = -1
queue_active = False
EARLY_AP_MODE = False
try:
    os.stat('force_ap.json')
    EARLY_AP_MODE = True
    try:
        os.remove('force_ap.json')
    except Exception:
        pass
except Exception:
    pass

try:
    if network.WLAN(network.AP_IF).active():
        EARLY_AP_MODE = True
except Exception:
    pass


def load_settings():
    try:
        with open('settings.json', 'r') as f:
            config = ujson.load(f)
            if not isinstance(config, dict):
                raise ValueError("Config is not a valid dictionary.")
    except (OSError, ValueError) as e:
        try:
            print(f"Error loading settings.json: {e}, loading default settings...")
        except OSError:
            pass
        config = {}
    return {
        "ssid": config.get("ssid", ""),
        "wifi_password": config.get("wifi_password", ""),
        "mqtt_server": config.get("mqtt_server", ""),
        "mqtt_username": config.get("mqtt_username", ""),
        "mqtt_password": config.get("mqtt_password", ""),
        "mqtt_enabled": int(config.get("mqtt_enabled", 0)),
        "cloud_enabled": int(config.get("cloud_enabled", 0)),
        "cloud_username": config.get("cloud_username", ""),
        "cloud_server": config.get("cloud_server", "sprinklers.intellidwell.net"),
        "cloud_port": int(config.get("cloud_port", 80)),
        "timezone": config.get("timezone", "-7"),
        "master_valve": int(config.get("master_valve", -1)),
        "ha_rain_delay": int(config.get("ha_rain_delay", 1 if not int(config.get("cloud_enabled", 0)) else 0)),
        "zone_names": config.get("zone_names", [f"Zone {i+1}" for i in range(10)]),
        "sequential_runs": config.get("sequential_runs", [])
    }

config = load_settings()

SSID = config['ssid']
PASSWORD = config['wifi_password']
MQTT_BROKER = config['mqtt_server']
MQTT_USER = config['mqtt_username']
MQTT_PASSWORD = config['mqtt_password']
MQTT = config.get('mqtt_enabled', 1) 
is_mqtt_connected = False

# Constants
RELAY_PINS = [13, 21, 14, 27, 26, 25, 33, 32, 19, 18]
relays = [Pin(pin, Pin.OUT) for pin in RELAY_PINS]
wifi = network.WLAN(network.STA_IF)
for relay in relays:
    relay.value(0)

def set_relay_value(pin, value):
    if 0 <= pin < len(relays):
        mv = int(config.get("master_valve", -1))
        if 0 <= mv < len(relays) and pin != mv:
            if value == 1 and relays[mv].value() == 0:
                relays[mv].value(1)
                log_message(f"Master valve (Relay {mv+1}) set to 1 (pre-delay)")
                if MQTT == 1:
                    try:
                        publish_relay_status(client, mv, 1)
                    except Exception:
                        pass
                time.sleep_ms(1000)
            relays[pin].value(value)
            expected = 1 if any(relays[i].value() == 1 for i in range(len(relays)) if i != mv) else 0
            if relays[mv].value() != expected:
                relays[mv].value(expected)
                log_message(f"Master valve (Relay {mv+1}) set to {expected}")
                if MQTT == 1:
                    try:
                        publish_relay_status(client, mv, expected)
                    except Exception:
                        pass
        else:
            relays[pin].value(value)
        trigger_cloud_sync(False)


def stop_all_watering():
    global timers, queue_active, run_queue, current_queue_index
    queue_active = False
    run_queue = []
    current_queue_index = -1
    timers.clear()
    for idx, relay in enumerate(relays):
        if relay.value() != 0:
            set_relay_value(idx, 0)
            if MQTT == 1:
                try:
                    publish_relay_status(client, idx, 0)
                except:
                    pass
    log_message("All watering stopped.")
    trigger_cloud_sync(False)


CLIENT_ID = "intellidwell_SC"
TOPIC_BASE = "home/sprinklers/"

client = None
if MQTT == 1 and MQTT_BROKER:
    try:
        from umqtt.simple import MQTTClient
        client = MQTTClient(CLIENT_ID, MQTT_BROKER, user=MQTT_USER, password=MQTT_PASSWORD, keepalive=60)
        client.set_last_will(f"{TOPIC_BASE}status", "Offline", retain=True)
    except Exception as e:
        try:
            print(f"Failed to initialize MQTTClient: {e}")
        except OSError:
            pass
        client = None

LOG_FILE = 'logs.txt'
LOG_MAX_LINES = 15
MAX_RECONNECT_ATTEMPTS = 10
_log_append_count = 0

def is_dst(year, month, day, weekday):
    """Determine if current date falls in US DST range."""
    if month == 3:
        second_sunday = 14 - (time.mktime((year, 3, 1, 0, 0, 0, 0, 0)) % 7)
        return day >= second_sunday
    if month == 11:
        first_sunday = 7 - (time.mktime((year, 11, 1, 0, 0, 0, 0, 0)) % 7)
        return day < first_sunday
    return 3 < month < 11

def save_settings(settings):
    global config
    if settings is None or not isinstance(settings, dict):
        raise ValueError("Invalid settings format. Expected a dictionary.")
    try:
        settings['mqtt_enabled'] = int(settings.get('mqtt_enabled', 0))  
        settings['cloud_enabled'] = int(settings.get('cloud_enabled', 0))
        settings['cloud_username'] = settings.get('cloud_username', '').strip()
        settings['master_valve'] = int(settings.get('master_valve', -1))
        if 'ha_rain_delay' in settings:
            settings['ha_rain_delay'] = 1 if int(settings.get('ha_rain_delay', 0)) else 0
        else:
            settings['ha_rain_delay'] = int(config.get('ha_rain_delay', 1 if not settings['cloud_enabled'] else 0))
        try:
            with open('settings.json', 'w') as f:
                ujson.dump(settings, f)
        except OSError as e:
            log_message(f"Flash write error saving settings: {e}")
        config = load_settings()
        log_message("Settings saved successfully.")
        trigger_cloud_sync(True)
    except Exception as e:
        log_message(f"Error saving settings: {e}")


_log_cache = []
try:
    _log_size = os.stat(LOG_FILE)[6]
    with open(LOG_FILE, 'r') as f:
        if _log_size > 2048:
            f.seek(_log_size - 2048)
            f.readline()  # discard partial first line
        for _ln in f:
            _ln = _ln.rstrip('\r\n')
            if _ln:
                _log_cache.append(_ln)
                if len(_log_cache) > LOG_MAX_LINES:
                    _log_cache.pop(0)
    if _log_size > 2048:
        with open(LOG_FILE, 'w') as f:
            for _ln in _log_cache:
                f.write(_ln + "\n")
except Exception:
    _log_cache = []
gc.collect()

def log_message(message):
    global _log_cache, _log_append_count
    current_time = time.localtime()
    timestamp = "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(*current_time[:6])
    log_entry = f"{timestamp}: {message}"
    
    _log_cache.append(log_entry)
    if len(_log_cache) > LOG_MAX_LINES:
        _log_cache = _log_cache[-LOG_MAX_LINES:]
        
    try:
        _log_append_count += 1
        if _log_append_count >= LOG_MAX_LINES:
            _log_append_count = 0
            with open(LOG_FILE, 'w') as f:
                for _ln in _log_cache:
                    f.write(_ln + "\n")
        else:
            with open(LOG_FILE, 'a') as f:
                f.write(log_entry + "\n")
    except (OSError, Exception):
        pass

    try:
        print(f"LOG: {log_entry}")
    except (OSError, Exception):
        pass


def load_rain_delay():
    try:
        with open("rain_delay.json") as f:
            return int(ujson.load(f).get("days_remaining", 0))
    except Exception:
        return 0

rain_delay_days = load_rain_delay()
weather_data = None

def get_rain_delay_days_remaining():
    global rain_delay_days
    return rain_delay_days

def set_rain_delay_days(days):
    global rain_delay_days
    try:
        try:
            with open("rain_delay.json", "w") as f:
                ujson.dump({"days_remaining": days}, f)
        except Exception:
            pass
        rain_delay_days = days
        log_message(f"Rain delay set to {days} days.")
        if MQTT == 1 and client is not None:
            try:
                client.publish("stat/rain_delay/state", str(days))
            except Exception:
                pass
        trigger_cloud_sync(False)
    except Exception as e:
        log_message(f"Failed to set rain delay: {e}")

def trigger_cloud_sync(full=False):
    if 'cloud_client' in globals() and cloud_client and cloud_client.is_connected():
        try:
            cloud_client.request_sync(full)
        except Exception:
            pass

def _get_timers_and_queue(expire=False):
    now = time.time()
    active_timers = {}
    for p, d in list(timers.items()):
        rem = int(d['end_time'] - now)
        if rem > 0:
            active_timers[str(p)] = rem
        elif expire:
            del timers[p]
            set_relay_value(p, 0)
            log_message(f"Zone {p + 1} timer expired. Relay turned OFF.")
            if MQTT == 1:
                publish_relay_status(client, p, 0)
    rem_q = 0
    if queue_active and 0 <= current_queue_index < len(run_queue):
        rem_q = max(0, int(run_queue[current_queue_index]['duration'] - (now - current_zone_start_time)))
    q_steps = [{"pin": item["pin"], "duration_mins": item["duration"] // 60} for item in run_queue]
    return active_timers, rem_q, q_steps

class ControllerAdapter:
    def get_zone_names(self):
        return list(config.get("zone_names", [f"Zone {i+1}" for i in range(10)]))
    def get_relay_values(self):
        return [r.value() for r in relays]
    def get_rain_delay_days(self):
        return get_rain_delay_days_remaining()
    def set_relay_value(self, pin, val):
        set_relay_value(pin, val)
    def set_rain_delay_days(self, days):
        set_rain_delay_days(int(days))
    def stop_all_watering(self):
        stop_all_watering()

    def get_sync_data(self, full=False):
        if full == "settings":
            return {
                "type": "sync",
                "settings": {
                    "master_valve": int(config.get("master_valve", -1)),
                    "timezone": str(config.get("timezone", "-7")),
                    "sequential_runs": config.get("sequential_runs", []),
                    "ssid": config.get("ssid", ""),
                    "mqtt_enabled": int(config.get("mqtt_enabled", 0)),
                    "mqtt_server": config.get("mqtt_server", ""),
                    "mqtt_username": config.get("mqtt_username", ""),
                    "ha_rain_delay": int(config.get("ha_rain_delay", 0))
                }
            }
        if full == "schedules":
            return {"type": "sync", "schedules": schedules}
        if full == "schedules_0":
            return {"type": "sync", "schedules_part": {"offset": 0, "items": schedules[:5]}}
        if full == "schedules_1":
            return {"type": "sync", "schedules_part": {"offset": 5, "items": schedules[5:10]}}
        active_timers, rem_q, q_steps = _get_timers_and_queue(False)
        sync_payload = {
            "type": "sync",
            "zones": self.get_zone_names(),
            "relays": self.get_relay_values(),
            "rain_delay": rain_delay_days,
            "firmware_version": FIRMWARE_VERSION,
            "timers": active_timers,
            "queue": {
                "active": queue_active,
                "current_index": current_queue_index,
                "remaining_seconds": rem_q,
                "steps": q_steps
            },
            "logs": _log_cache[-6:]
        }
        if weather_data:
            sync_payload["weather"] = weather_data
        return sync_payload

    def start_zone(self, pin, runtime_secs=0):
        pin = int(pin)
        runtime_secs = int(runtime_secs or 0)
        if 0 <= pin < len(relays):
            stop_all_watering()
            if runtime_secs > 0:
                timers[pin] = {'end_time': time.time() + runtime_secs}
                set_relay_value(pin, 1)
                log_message(f"Zone {pin + 1} turned ON (timer set for {max(1, runtime_secs // 60)} minutes).")
            else:
                set_relay_value(pin, 1)
                log_message(f"Zone {pin + 1} manually turned ON.")
            if MQTT == 1:
                publish_relay_status(client, pin, 1)
            trigger_cloud_sync(False)
            return True
        return False

    def stop_zone(self, pin):
        stop_all_watering()

    def start_queue(self, queue_items):
        global run_queue, current_queue_index, queue_active
        stop_all_watering()
        run_queue = []
        for item in (queue_items or []):
            p = int(item.get('pin', 0))
            mins = int(item.get('duration_mins', 0))
            if 0 <= p < len(relays) and mins > 0:
                run_queue.append({"pin": p, "duration": mins * 60})
        if run_queue:
            queue_active = True
            current_queue_index = 0
            log_message(f"Starting sequential runs for {len(run_queue)} zones.")
            trigger_cloud_sync(False)
            return True
        return False

    def schedule_op(self, op, pin, payload):
        pin = int(pin)
        if not (0 <= pin < len(schedules)):
            return False
        if op == "toggle_zone":
            st = bool(payload.get("enabled", True))
            schedules[pin]['enabled'] = st
            save_schedules(schedules)
            publish_schedule_status(client, pin, st)
            log_message(f"Schedule for Relay {pin+1} {'enabled' if st else 'disabled'}!")
            return True
        sched_id = payload.get("schedule_id") or payload.get("id")
        if op == "delete":
            orig = len(schedules[pin]['schedules'])
            schedules[pin]['schedules'] = [s for s in schedules[pin]['schedules'] if s.get('id') != sched_id]
            if len(schedules[pin]['schedules']) < orig:
                save_schedules(schedules)
                log_message(f"Deleted schedule {sched_id} from Zone {pin+1}")
                return True
            return False
        if op == "toggle_item":
            en = bool(payload.get("enabled", True))
            for s in schedules[pin]['schedules']:
                if s.get('id') == sched_id:
                    s['enabled'] = en
                    save_schedules(schedules)
                    log_message(f"Schedule {sched_id} on Zone {pin+1} set to {en}")
                    return True
            return False
        if pin == int(config.get("master_valve", -1)):
            return False
        days = payload.get("days", [])
        on_t = payload.get("onTime") or payload.get("start_time")
        off_t = payload.get("offTime") or payload.get("end_time")
        if not days or not on_t or not off_t:
            return False
        if sched_id and op != "add":
            for s in schedules[pin]['schedules']:
                if s.get('id') == sched_id:
                    s['days'] = days
                    s['onTime'] = on_t
                    s['offTime'] = off_t
                    if 'enabled' in payload:
                        s['enabled'] = bool(payload['enabled'])
                    save_schedules(schedules)
                    log_message(f"Edited schedule {sched_id} on Zone {pin+1}")
                    return True
            if op == "edit":
                return False
        import random
        new_id = sched_id or f"sch_{int(time.time())}_{random.randint(1000, 9999)}"
        new_sched = {
            'id': new_id,
            'days': days,
            'onTime': on_t,
            'offTime': off_t,
            'enabled': bool(payload.get('enabled', True))
        }
        schedules[pin]['schedules'].append(new_sched)
        save_schedules(schedules)
        log_message(f"Added schedule {new_id} to Zone {pin+1}")
        return new_sched

    def update_config(self, cmd_type, payload):
        act = payload.get("action")
        if act == "start_run_queue":
            return self.start_queue(payload.get("queue", []))
        if act == "stop_run_queue":
            stop_all_watering()
            return True
        if act == "clear_logs":
            return self.clear_logs()
        cfg = dict(config)
        if cmd_type == "update_zone" or ("zone_id" in payload and "name" in payload):
            zid = int(payload.get("zone_id", -1))
            names = list(cfg.get("zone_names", [f"Zone {i+1}" for i in range(10)]))
            if 0 <= zid < len(names):
                names[zid] = str(payload.get("name", "")).strip() or f"Zone {zid+1}"
                cfg["zone_names"] = names
        if isinstance(payload.get("zone_names"), list):
            cfg["zone_names"] = [str(x).strip() or f"Zone {i+1}" for i, x in enumerate(payload["zone_names"][:10])]
        if cmd_type == "update_master_valve" or "master_valve" in payload:
            if "enabled" in payload and not payload["enabled"]:
                cfg["master_valve"] = -1
            else:
                mv = payload.get("master_valve", payload.get("output_id", -1))
                cfg["master_valve"] = int(mv) if mv is not None else -1
        if "timezone" in payload:
            cfg["timezone"] = str(payload["timezone"])
        if "ha_rain_delay" in payload:
            cfg["ha_rain_delay"] = 1 if payload.get("ha_rain_delay") else 0
        if isinstance(payload.get("sequential_runs"), list):
            cfg["sequential_runs"] = payload["sequential_runs"]
        if "ssid" in payload and str(payload["ssid"]).strip():
            cfg["ssid"] = str(payload["ssid"]).strip()
            if payload.get("wifi_password"):
                cfg["wifi_password"] = str(payload["wifi_password"])
        if cmd_type == "update_mqtt" or "mqtt_enabled" in payload:
            cfg["mqtt_enabled"] = 1 if payload.get("mqtt_enabled", payload.get("enabled")) else 0
            for k, alt in (("mqtt_server", "host"), ("mqtt_username", "username"), ("mqtt_password", "password")):
                v = payload.get(k, payload.get(alt))
                if v is not None and (k != "mqtt_password" or v != ""):
                    cfg[k] = str(v).strip()
        save_settings(cfg)
        return True

    def clear_logs(self):
        global _log_cache
        _log_cache = []
        try:
            with open(LOG_FILE, 'w') as f:
                f.write("")
        except Exception:
            pass
        log_message("Logs cleared.")
        trigger_cloud_sync(True)
        return True

    def start_ota(self, version):
        if ota_progress < 0 or ota_progress >= 100:
            asyncio.create_task(run_ota_update_task(version or 'beta'))

    def reset(self):
        async def _do_rst():
            await asyncio.sleep(1)
            reset()
        asyncio.create_task(_do_rst())

_ctrl_adapter = ControllerAdapter()
try:
    cloud_client = CloudClient(config, _ctrl_adapter, log_message, ver_str=FIRMWARE_VERSION) if CloudClient else None
except Exception as e:
    log_message(f"CloudClient init failed: {e}")
    cloud_client = None

def command_callback(topic, msg):
    topic = topic.decode()
    msg = msg.decode()
    log_message(f"Command received: Topic: {topic}, Message: {msg}")
    parts = topic.split('/')
    
    if len(parts) == 4 and parts[0] == "cmnd" and parts[1] == "zone" and parts[3] == "power":
        pin = int(parts[2])
        if msg == "ON":
            set_relay_value(pin, 1)
            log_message(f"Relay {pin} turned ON via MQTT")
        elif msg == "OFF":
            set_relay_value(pin, 0)
            log_message(f"Relay {pin} turned OFF via MQTT")
        if MQTT == 1 and client is not None:
            publish_relay_status(client, pin, relays[pin].value())
    elif len(parts) == 4 and parts[1] == "zone" and parts[3] == "schedule":
        pin = int(parts[2])
        status = msg.lower() == "true"
        if not _ctrl_adapter.schedule_op("toggle_zone", pin, {"enabled": status}):
            log_message("Invalid schedule operation")
    elif topic == "cmnd/rain_delay/set":
        try:
            set_rain_delay_days(int(msg))
        except Exception as e:
            log_message(f"Failed to set rain delay via MQTT: {e}")
    elif topic == "cmnd/restart" and msg == "RESTART":
        log_message("Restart command received via MQTT. Rebooting...")
        reset()

if client is not None:
    try:
        client.set_callback(command_callback)
    except Exception as e:
        log_message(f"Failed to set MQTT callback: {e}")

async def connect_mqtt():
    global is_mqtt_connected
    try:
        if MQTT != 1 or client is None:
            is_mqtt_connected = False
            return
        client.connect()
        is_mqtt_connected = True
        log_message("MQTT connected.")
        await subscribe_to_topics()
    except Exception as e:
        is_mqtt_connected = False
        log_message(f"Failed to connect to MQTT at startup: {e}")

async def subscribe_to_topics():
    if client is None:
        return
    try:
        try:
            client.publish(f"{TOPIC_BASE}status", "Online", retain=True)
        except Exception:
            pass
        def _pub_disc(topic, dev_id, dev_name, extra):
            p = {
                "device": {"identifiers": [dev_id], "name": dev_name, "manufacturer": "Intellidwell", "model": "Sprinkler Controller V1.0", "sw_version": "1.0"},
                "availability_topic": f"{TOPIC_BASE}status",
                "payload_available": "Online",
                "payload_not_available": "Offline",
                "platform": "mqtt"
            }
            p.update(extra)
            client.publish(topic, ujson.dumps(p), retain=True)

        for i in range(len(relays)):
            client.subscribe(f"cmnd/zone/{i}/power")
            client.subscribe(f"cmnd/zone/{i}/schedule")
            _pub_disc(f"homeassistant/switch/SC{i}/config", "intellidwellSC", "Sprinkler Controller", {
                "name": f"Zone {i + 1}", "command_topic": f"cmnd/zone/{i}/power", "state_topic": f"stat/zone/{i}/state",
                "payload_on": "ON", "payload_off": "OFF", "unique_id": f"intellidwellSC{i}"
            })
            _pub_disc(f"homeassistant/switch/SS{i}/config", "intellidwellSS", "Sprinkler Scheduler", {
                "name": f"Zone {i + 1} Scheduler", "command_topic": f"cmnd/zone/{i}/schedule", "state_topic": f"stat/zone/{i}/schedule",
                "payload_on": "true", "payload_off": "false", "unique_id": f"intellidwellSS{i}"
            })
        client.subscribe("cmnd/rain_delay/set")
        client.subscribe("cmnd/restart")
        _pub_disc("homeassistant/number/rain_delay/config", "intellidwellSC", "Sprinkler Controller", {
            "name": "Rain Delay", "command_topic": "cmnd/rain_delay/set", "state_topic": "stat/rain_delay/state",
            "min": 0, "max": 5, "step": 1, "unique_id": "intellidwell_rain_delay"
        })
        _pub_disc("homeassistant/button/intellidwell_restart/config", "intellidwellSC", "Sprinkler Controller", {
            "name": "Restart", "command_topic": "cmnd/restart", "payload_press": "RESTART", "unique_id": "intellidwell_restart_btn"
        })
        try:
            client.publish("stat/rain_delay/state", str(get_rain_delay_days_remaining()))
        except Exception:
            pass
        for i in range(len(relays)):
            try:
                publish_relay_status(client, i, relays[i].value())
                publish_schedule_status(client, i, schedules[i].get("enabled", True))
            except Exception:
                pass
    except Exception as e:
        log_message(f"Failed to subscribe to topics: {e}")

async def check_messages():
    global is_mqtt_connected
    while True:
        try:
            if MQTT == 1 and client is not None:
                if not is_mqtt_connected:
                    log_message("MQTT not connected. Attempting connection...")
                    try:
                        try:
                            if getattr(client, 'sock', None):
                                client.sock.close()
                                client.sock = None
                        except Exception:
                            pass
                        await asyncio.sleep(2)
                        client.connect()
                        is_mqtt_connected = True
                        log_message("MQTT connected.")
                        await subscribe_to_topics()
                    except Exception as e:
                        is_mqtt_connected = False
                        log_message(f"Failed to connect to MQTT: {e}")
                        try:
                            if getattr(client, 'sock', None):
                                client.sock.close()
                                client.sock = None
                        except Exception:
                            pass
                        await asyncio.sleep(15)
                        continue
                
                try:
                    client.check_msg()
                except Exception as e:
                    log_message(f"MQTT connection lost or check_msg failed: {e}")
                    is_mqtt_connected = False
                    try:
                        if getattr(client, 'sock', None):
                            client.sock.close()
                            client.sock = None
                    except Exception:
                        pass
        except Exception as e:
            log_message(f"Error in check_messages: {e}")
            is_mqtt_connected = False
            
        await asyncio.sleep(1)
        gc.collect()  # Run garbage collection to free up memory

async def mqtt_ping_loop():
    global is_mqtt_connected
    while True:
        try:
            if MQTT == 1 and client is not None and is_mqtt_connected:
                client.ping()
        except Exception as e:
            log_message(f"MQTT ping failed: {e}")
            is_mqtt_connected = False
            try:
                if getattr(client, 'sock', None):
                    client.sock.close()
                    client.sock = None
            except Exception:
                pass
        await asyncio.sleep(20)

# Helper to monitor memory usage
_last_mem_warning = 0
def monitor_memory():
    global _last_mem_warning
    gc.collect()
    free_memory = gc.mem_free()
    if free_memory < 4000:
        now_ts = time.time()
        if now_ts - _last_mem_warning > 300:
            _last_mem_warning = now_ts
            log_message(f"Low memory warning! Free: {free_memory} bytes")

# Web server app
app = Microdot()

CORS_HEADERS = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type, X-Filename',
    'Access-Control-Allow-Private-Network': 'true'
}
JSON_HEADERS = {
    'Content-Type': 'application/json',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Private-Network': 'true'
}

@app.before_request
def handle_options(request):
    if request.method == 'OPTIONS':
        return '', 200, CORS_HEADERS

@app.after_request
def cleanup(request, response):
    if response and hasattr(response, 'headers'):
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Private-Network'] = 'true'
    gc.collect()
    return response

@app.route('/logs-page')
@app.route('/logs.html')
def logs_page(request):
    gc.collect()
    return send_file('logs.html')

@app.route('/get-logs')
def get_logs(request):
    try:
        return "\n".join(_log_cache), {'Content-Type': 'text/plain'}
    except Exception as e:
        log_message(f"Error reading logs: {e}")
        return 'Error reading logs', 500


@app.route('/clear-logs', methods=['POST'])
def clear_logs(request):
    _ctrl_adapter.clear_logs()
    return 'Logs cleared', 200

@app.route('/restart', methods=['POST', 'OPTIONS'])
async def restart(request):
    log_message("Restarting device in 1 second...")
    _ctrl_adapter.reset()
    return 'Restarting...', 200


@app.route('/')
@app.route('/index.html')
def index(request):
    gc.collect()
    return send_file('index.html')

@app.route('/settings')
@app.route('/settings.html')
def settings(request):
    gc.collect()
    return send_file('settings.html')

@app.route('/get-settings', methods=['GET', 'OPTIONS'])
def get_settings(request):
    gc.collect()
    response_data = dict(config)
    response_data["firmware_version"] = FIRMWARE_VERSION
    return ujson.dumps(response_data), 200, JSON_HEADERS

@app.route('/edit-schedule/<pin>/<sched_id>', methods=['POST', 'OPTIONS'])
def edit_schedule(request, pin, sched_id):
    try:
        data = dict(request.json or {})
        data['id'] = sched_id
        if _ctrl_adapter.schedule_op("edit", pin, data):
            return 'Updated', 200
        return 'Schedule not found or invalid', 400
    except Exception as e:
        log_message(f"Error editing schedule: {e}")
        return 'Failed to edit schedule', 500

@app.route('/start-run-queue', methods=['POST', 'OPTIONS'])
def start_run_queue(request):
    try:
        data = request.json
        if data and 'queue' in data and _ctrl_adapter.start_queue(data['queue']):
            return 'Queue started', 200
        return 'Queue is empty or invalid', 400
    except Exception as e:
        log_message(f"Error starting run queue: {e}")
        return 'Error', 500

@app.route('/stop-run-queue', methods=['POST', 'OPTIONS'])
def stop_run_queue_route(request):
    stop_all_watering()
    return 'Queue stopped', 200

@app.route('/get-run-queue-status', methods=['GET', 'OPTIONS'])
def get_run_queue_status(request):
    _, rem_q, q_steps = _get_timers_and_queue(False)
    return ujson.dumps({
        "active": queue_active,
        "current_index": current_queue_index,
        "remaining_seconds": rem_q,
        "queue": q_steps
    }), 200, JSON_HEADERS

@app.route('/ota/upload', methods=['POST', 'OPTIONS'])
def ota_upload(request):
    gc.collect()
    filename = request.headers.get('X-Filename')
    if not filename:
        return 'Missing X-Filename header', 400
        
    try:
        filename = filename.split('/')[-1].split('\\')[-1]
        tmp_filename = filename + '.tmp'
        with open(tmp_filename, 'wb') as f:
            f.write(request.body)
        try:
            os.remove(filename)
        except OSError:
            pass
        os.rename(tmp_filename, filename)
        
        log_message(f"OTA: Uploaded and activated file {filename} ({len(request.body)} bytes)")
        return 'File uploaded successfully', 200
    except Exception as e:
        log_message(f"OTA: Failed to upload file {filename}: {e}")
        try:
            os.remove(tmp_filename)
        except:
            pass
        return f'Error: {e}', 500

ota_progress = -1
ota_logs = []
ota_complete = False
ota_error = None

async def run_ota_update_task(version):
    global ota_progress, ota_logs, ota_complete, ota_error
    
    try:
        import gc
        import urequests
        import os
        
        ota_progress = 5
        ota_complete = False
        ota_error = None
        ota_logs = [f"Starting firmware pull update (Version: {version}) from Intellidwell..."]
        
        cfg = config
        update_server = cfg.get("update_server", "http://www.intellidwell.net").strip()
        if not update_server:
            update_server = "http://www.intellidwell.net"
        if update_server.startswith("https://"):
            update_server = "http://" + update_server[8:]
        req_headers = {}
        try:
            if wifi.isconnected() and wifi.ifconfig()[0].startswith("192.168.1.") and "intellidwell.net" in update_server:
                update_server = "http://192.168.1.74"
                req_headers = {"Host": "www.intellidwell.net"}
        except Exception:
            pass
            
        if version == 'stable':
            BASE_URL = f"{update_server}/static/sprinkler_src"
        elif version == 'beta':
            BASE_URL = f"{update_server}/static/sprinkler_src_beta"
        elif version == 'deprecated':
            BASE_URL = f"{update_server}/static/sprinkler_src_deprecated"
        else:
            if version.startswith('2.1'):
                BASE_URL = f"{update_server}/static/firmware/beta/{version}"
            elif version.startswith('2.0'):
                BASE_URL = f"{update_server}/static/firmware/stable/{version}"
            else:
                BASE_URL = f"{update_server}/static/firmware/deprecated/{version}"
            
        files = ["controller.mpy", "main.py", "index.html", "logs.html", "scheduler.html", "settings.html", "microdot.mpy", "microdot_asyncio.mpy"]
        if version != 'deprecated' and not version.startswith('1.'):
            files.append("cloud.mpy")
        total_files = len(files)

        async def _download_ota_file(dl_url, dst_path):
            res = None
            try:
                try:
                    res = urequests.get(dl_url, headers=req_headers, stream=True, timeout=10)
                except TypeError:
                    res = urequests.get(dl_url, headers=req_headers, stream=True)
                if res.status_code != 200:
                    code = res.status_code
                    res.close()
                    return code
                raw = getattr(res, 'raw', None)
                if raw is not None and hasattr(raw, 'settimeout'):
                    try:
                        raw.settimeout(10.0)
                    except Exception:
                        pass
                expected = None
                try:
                    hdrs = getattr(res, 'headers', None) or {}
                    for hk, hv in hdrs.items():
                        if str(hk).lower() == 'content-length':
                            expected = int(hv)
                            break
                except Exception:
                    pass
                tmp_path = dst_path + '.tmp'
                read_bytes = 0
                with open(tmp_path, 'wb') as f:
                    while expected is None or read_bytes < expected:
                        to_read = 512 if expected is None else min(512, expected - read_bytes)
                        chunk = raw.read(to_read) if raw is not None else res.content
                        if not chunk:
                            break
                        f.write(chunk)
                        read_bytes += len(chunk)
                        if raw is None:
                            break
                        await asyncio.sleep_ms(2)
                res.close()
                res = None
                if expected is not None and read_bytes < expected:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                    raise Exception(f"Truncated download ({read_bytes}/{expected})")
                try:
                    os.remove(dst_path)
                except OSError:
                    pass
                os.rename(tmp_path, dst_path)
                return 200
            finally:
                if res is not None:
                    try:
                        res.close()
                    except Exception:
                        pass

        if 'cloud_client' in globals() and cloud_client:
            try:
                cloud_client._running = False
                cloud_client._connected = False
                if cloud_client.writer:
                    w = cloud_client.writer
                    cloud_client.writer = None
                    from cloud import _close_stream
                    await _close_stream(w)
            except Exception:
                pass
        gc.collect()
        for index, filename in enumerate(files):
            ota_logs.append(f"Downloading {filename}...")
            log_message(f"OTA: Downloading {filename}...")
            ota_progress = 10 + int((index / total_files) * 80)
            gc.collect()
            await asyncio.sleep(0.1)  # Cooperatively yield control

            code = await _download_ota_file(f"{BASE_URL}/{filename}", filename)
            if code == 200:
                if filename.endswith('.html'):
                    try:
                        os.remove(filename + '.gz')
                    except OSError:
                        pass
                ota_logs.append(f"Successfully updated {filename}.")
                ota_progress = 10 + int(((index + 1) / total_files) * 80)
            elif filename == "cloud.mpy" and code == 404:
                ota_progress = 10 + int(((index + 1) / total_files) * 80)
            else:
                raise Exception(f"HTTP Status {code}")

            gc.collect()
            await asyncio.sleep(0.1)  # Cooperatively yield control

        # Optional precompressed pages + version marker (non-fatal if missing on server)
        gz_ver = ''
        try:
            r = urequests.get(f"{BASE_URL}/.gz_fw", headers=req_headers)
            if r.status_code == 200:
                gz_ver = r.text.strip()
            r.close()
        except Exception:
            pass
        if gz_ver:
            for html in ("index.html", "logs.html", "scheduler.html", "settings.html"):
                try:
                    await _download_ota_file(f"{BASE_URL}/{html}.gz", html + '.gz')
                except Exception:
                    pass
                gc.collect()
                await asyncio.sleep(0.1)
            try:
                with open('.gz_ver', 'w') as f:
                    f.write(gz_ver)
            except Exception:
                pass

        ota_logs.append("All files updated successfully. Scheduling reboot in 5 seconds...")
        ota_progress = 95
        await asyncio.sleep(5)
        ota_progress = 100
        ota_complete = True
        await asyncio.sleep(3)  # Allow time for browser to poll the final 100% status
        reset()
    except Exception as e:
        ota_error = str(e)
        ota_logs.append(f"[ERROR] Task failed: {ota_error}")
        ota_progress = -1

@app.route('/ota/pull', methods=['POST', 'OPTIONS'])
def ota_pull(request):
    global ota_progress
    if ota_progress >= 0 and ota_progress < 100:
        return 'Update already in progress', 400
    version = request.args.get('version', 'stable') if request.args else 'stable'
    asyncio.create_task(run_ota_update_task(version))
    return ujson.dumps({'status': 'started'}), 200, JSON_HEADERS

@app.route('/ota/status', methods=['GET', 'OPTIONS'])
def ota_status(request):
    global ota_progress, ota_logs, ota_complete, ota_error
    return ujson.dumps({
        'progress': ota_progress,
        'logs': ota_logs,
        'complete': ota_complete,
        'error': ota_error
    }), 200, JSON_HEADERS


@app.route('/save-settings', methods=['POST'])
def save_settings_route(request):
    settings = request.json
    if settings is None:
        return 'Invalid settings data', 400
    try:
        save_settings(settings)
        return 'Settings saved successfully', 200
    except ValueError as e:
        log_message(f"Error saving settings: {e}")
        return 'Failed to save settings', 500


@app.route('/api/cloud/status', methods=['GET', 'OPTIONS'])
def api_cloud_status(request):
    info = cloud_client.get_status_info() if cloud_client else {"enabled": False, "connected": False, "status": "disabled"}
    return ujson.dumps(info), 200, JSON_HEADERS


@app.route('/api/cloud/login', methods=['POST', 'OPTIONS'])
async def api_cloud_login(request):
    if not cloud_client:
        return ujson.dumps({"success": False, "message": "Cloud module not available"}), 400, JSON_HEADERS
    data = request.json or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    if not username or not password:
        return ujson.dumps({"success": False, "message": "Username and password required"}), 400, JSON_HEADERS

    ok, msg = await cloud_client.login(username, password)
    if ok:
        config['cloud_enabled'] = 1
        config['cloud_username'] = username
        try:
            with open('settings.json', 'w') as f:
                ujson.dump(config, f)
        except Exception:
            pass
        if not cloud_client._running:
            asyncio.create_task(cloud_client.loop())
        return ujson.dumps({"success": True, "message": msg}), 200, JSON_HEADERS
    else:
        return ujson.dumps({"success": False, "message": msg}), 401, JSON_HEADERS


@app.route('/get-relay-states')
def get_relay_states(request):
    states = [relay.value() for relay in relays]
    return ujson.dumps(states), JSON_HEADERS

@app.route('/scheduler')
@app.route('/scheduler.html')
def scheduler(request):
    gc.collect()
    return send_file('scheduler.html')

@app.route('/set-schedule/<pin>', methods=['POST'])
def set_schedule(request, pin):
    try:
        pin = int(pin)
        if pin == int(config.get("master_valve", -1)):
            return 'Cannot schedule master valve zone', 400
        res = _ctrl_adapter.schedule_op("add", pin, {
            "days": request.form.getlist('day'),
            "onTime": request.form.get('onTime'),
            "offTime": request.form.get('offTime'),
            "enabled": True
        })
        if res:
            return f'Schedule set for Relay {pin + 1}!'
        return 'Failed to set schedule', 400
    except Exception as e:
        log_message(f"Error setting schedule: {e}")
        return 'Failed to set schedule', 500

@app.route('/add-schedule/<pin>', methods=['POST'])
def add_schedule(request, pin):
    try:
        res = _ctrl_adapter.schedule_op("add", pin, request.json or {})
        if isinstance(res, dict):
            return ujson.dumps(res), 200, JSON_HEADERS
        return 'Invalid schedule data', 400
    except Exception as e:
        log_message(f"Error adding schedule: {e}")
        return 'Failed to add schedule', 500

@app.route('/delete-schedule/<pin>/<sched_id>', methods=['POST'])
def delete_schedule(request, pin, sched_id):
    try:
        if _ctrl_adapter.schedule_op("delete", pin, {"id": sched_id}):
            return 'Deleted', 200
        return 'Schedule not found', 404
    except Exception as e:
        log_message(f"Error deleting schedule: {e}")
        return 'Failed to delete schedule', 500

@app.route('/toggle-schedule-item/<pin>/<sched_id>/<state>', methods=['POST'])
def toggle_schedule_item(request, pin, sched_id, state):
    try:
        if _ctrl_adapter.schedule_op("toggle_item", pin, {"id": sched_id, "enabled": state.lower() == 'true'}):
            return 'Toggled', 200
        return 'Schedule not found', 404
    except Exception as e:
        log_message(f"Error toggling schedule item: {e}")
        return 'Failed to toggle schedule item', 500

@app.route('/toggle-schedule/<pin>/<status>', methods=['GET'])
def toggle_schedule(request, pin, status):
    st = status.lower() == 'true'
    if _ctrl_adapter.schedule_op("toggle_zone", pin, {"enabled": st}):
        return f"Schedule for Relay {int(pin)+1} {'enabled' if st else 'disabled'}!"
    return 'Invalid pin or status', 400

@app.route('/relay-timer/<pin>/<minutes>', methods=['GET', 'OPTIONS'])
def start_timer(request, pin, minutes):
    try:
        mins = int(minutes)
        if mins > 0 and _ctrl_adapter.start_zone(pin, mins * 60):
            return 'Timer started', 200
        return 'Invalid input', 400
    except Exception as e:
        log_message(f"Error in /relay-timer route: {e}")
        return 'Failed to start timer', 500

@app.route('/timer-status')
def timer_status(request):
    try:
        status, _, _ = _get_timers_and_queue(True)
        return ujson.dumps(status), JSON_HEADERS
    except Exception as e:
        log_message(f"Error in /timer-status: {e}")
        return '{}', 500


@app.route('/api/status', methods=['GET', 'OPTIONS'])
def api_status(request):
    try:
        status_dict, remaining, formatted_queue = _get_timers_and_queue(False)
        payload = {
            "firmware_version": FIRMWARE_VERSION,
            "relay_states": [relay.value() for relay in relays],
            "timers": status_dict,
            "queue": {
                "active": queue_active,
                "current_index": current_queue_index,
                "remaining_seconds": remaining,
                "steps": formatted_queue
            }
        }
        if weather_data:
            payload["weather"] = weather_data
        return ujson.dumps(payload), 200, JSON_HEADERS
    except Exception as e:
        log_message(f"Error in /api/status route: {e}")
        return '{}', 500, JSON_HEADERS


@app.route('/api/weather', methods=['POST', 'OPTIONS'])
def api_weather(request):
    global weather_data
    if request.method == 'OPTIONS':
        return '', 200, {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'POST, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type'
        }
    try:
        data = request.json or {}
        if not isinstance(data, dict):
            return 'Invalid payload', 400
        rain_today = float(data.get("rain_today_in", data.get("rain_today", 0.0)))
        source = str(data.get("source", "Weather Station"))
        delay_days = int(data.get("delay_days", data.get("rain_delay", 0)))
        weather_data = {
            "rain_today_in": rain_today,
            "source": source,
            "delay_days": delay_days,
            "temp": data.get("temp"),
            "updated_at": time.time()
        }
        allow_ha_delay = (int(config.get("cloud_enabled", 0)) == 0) or (int(config.get("ha_rain_delay", 0)) == 1)
        if delay_days > 0 and allow_ha_delay:
            set_rain_delay_days(delay_days)
            log_message(f"Rain delay {delay_days}d set via weather push ({source}, rain: {rain_today}in)")
        else:
            trigger_cloud_sync(False)
        return ujson.dumps({"status": "ok", "weather": weather_data}), 200, JSON_HEADERS
    except Exception as e:
        log_message(f"Error in /api/weather: {e}")
        return 'Internal error', 500


@app.route('/cancel-timer/<pin>', methods=['GET', 'OPTIONS'])
def cancel_timer(request, pin):
    try:
        pin = int(pin)
        stop_all_watering()
        return 'Timer canceled', 200
    except Exception as e:
        log_message(f"Error in /cancel-timer route: {e}")
        return 'Failed to cancel timer', 500


@app.route('/relay/<int:pin>/<state>', methods=['GET', 'OPTIONS'])
async def relay_toggle(request, pin, state):
    try:
        if 0 <= pin < len(relays):
            stop_all_watering()
            if state == 'on':
                set_relay_value(pin, 1)
                log_message(f"Zone {pin + 1} manually turned ON.")
                if MQTT == 1:
                    publish_relay_status(client, pin, 1)
            else:
                set_relay_value(pin, 0)
                log_message(f"Zone {pin + 1} manually turned OFF.")
                if MQTT == 1:
                    publish_relay_status(client, pin, 0)
            return 'OK', 200
        return 'Invalid pin', 400
    except Exception as e:
        log_message(f"Error in /relay route: {e}")
        return 'Error', 500

@app.route('/set-rain-delay/<days>')
def set_rain_delay(request, days):
    try:
        days = int(days)
        set_rain_delay_days(days)
        return "Rain delay set", 200
    except:
        return "Invalid input", 400
    
@app.route('/get-rain-delay')
def get_rain_delay(request):
    try:
        days = get_rain_delay_days_remaining()
        return ujson.dumps({"days_remaining": days}), JSON_HEADERS
    except:
        return ujson.dumps({"days_remaining": 0}), JSON_HEADERS


@app.route('/get-schedules')
def get_schedules(request):
    gc.collect()
    try:
        os.stat('schedules.json')
        return send_file('schedules.json', content_type='application/json')
    except Exception:
        return ujson.dumps(schedules), JSON_HEADERS


def load_schedules():
    try:
        with open('schedules.json', 'r') as f:
            data = ujson.load(f)
        if not isinstance(data, list):
            return [{"enabled": True, "schedules": []} for _ in RELAY_PINS]
        out = []
        for i in range(len(relays)):
            it = data[i] if i < len(data) else None
            if isinstance(it, dict):
                en = bool(it.get("enabled", True))
                sc = it.get("schedules")
                if not isinstance(sc, list):
                    sc = [{"id": f"sch_{i}", "days": it.get("days", []), "onTime": it.get("onTime", ""), "offTime": it.get("offTime", ""), "enabled": True}] if ("onTime" in it or "offTime" in it) else []
                out.append({"enabled": en, "schedules": sc})
            elif isinstance(it, list):
                out.append({"enabled": True, "schedules": it})
            else:
                out.append({"enabled": True, "schedules": []})
        return out
    except (OSError, ValueError):
        return [{"enabled": True, "schedules": []} for _ in RELAY_PINS]

def save_schedules(schedules_data):
    try:
        with open('schedules.json', 'w') as f:
            ujson.dump(schedules_data, f)
        trigger_cloud_sync(True)
    except Exception as e:
        log_message(f"Failed to save schedules: {e}")

# Load or create a schedule store
gc.collect()
schedules = load_schedules()
gc.collect()

def _configure_sta_rf(wifi):
    try:
        wifi.config(pm=0)
    except Exception:
        pass
    try:
        wifi.config(txpower=15)
    except Exception:
        pass
    if not wifi.isconnected():
        try:
            wifi.config(protocol=network.MODE_11B)
        except Exception:
            pass
        try:
            wifi.config(bandwidth=1)
        except Exception:
            pass

def _connect_sta(wifi, ssid, password):
    _configure_sta_rf(wifi)
    best_bssid = None
    try:
        target = ssid.encode() if isinstance(ssid, str) else ssid
        best_rssi = -999
        for ap in wifi.scan():
            if ap[0] == target and ap[3] > best_rssi:
                best_rssi = ap[3]
                best_bssid = ap[1]
    except Exception:
        pass
    if best_bssid:
        wifi.connect(ssid, password, bssid=best_bssid)
    else:
        wifi.connect(ssid, password)

async def connect_to_wifi(attempts=2, timeout_per_attempt=12):
    if not SSID or SSID == "":
        log_message("SSID is not configured.")
        return False

    try:
        try:
            network.hostname("sprinklers")
        except Exception:
            pass
        wifi = network.WLAN(network.STA_IF)
        try:
            wifi.config(dhcp_hostname="sprinklers")
        except Exception:
            pass
        if not wifi.active():
            wifi.active(True)
        try:
            wifi.config(dhcp_hostname="sprinklers")
        except Exception:
            pass
    except Exception as e:
        log_message(f"Error initializing Wi-Fi interface: {e}")

    wifi = network.WLAN(network.STA_IF)

    for attempt in range(1, attempts + 1):
        try:
            if wifi.isconnected() and wifi.ifconfig()[0] != '0.0.0.0':
                try:
                    network.hostname("sprinklers")
                except Exception:
                    pass
                try:
                    wifi.config(dhcp_hostname="sprinklers")
                except Exception:
                    pass
                try:
                    wifi.config(pm=0)
                except Exception:
                    pass
                return True

            gc.collect()
            log_message(f"Connecting to Wi-Fi '{SSID}' (attempt {attempt}/{attempts})...")
            if wifi.status() != 1001:
                _connect_sta(wifi, SSID, PASSWORD)

            # Wait up to timeout_per_attempt seconds, polling every 500ms
            poll_count = int(timeout_per_attempt / 0.5)
            for _ in range(poll_count):
                if wifi.isconnected() and wifi.ifconfig()[0] != '0.0.0.0':
                    break
                await asyncio.sleep(0.5)

            if wifi.isconnected() and wifi.ifconfig()[0] != '0.0.0.0':
                log_message('Connected to Wi-Fi as sprinklers.local')
                try:
                    wifi.config(pm=0)
                except Exception:
                    pass
                try:
                    print('Wifi connected as sprinklers.local, net={}, gw={}, dns={}'.format(*wifi.ifconfig()))
                except OSError:
                    pass
                return True
            else:
                log_message(f"Wi-Fi connection attempt {attempt} failed.")
                try:
                    wifi.disconnect()
                except Exception:
                    pass
                await asyncio.sleep(1.5)
                gc.collect()
        except Exception as e:
            log_message(f"Exception during Wi-Fi connection attempt {attempt}: {e}")
            try:
                wifi.disconnect()
            except Exception:
                pass
            await asyncio.sleep(1.5)

    return False

@app.route("/api/time")
def get_rtc_time(request):
    rtc = machine.RTC().datetime()
    return ujson.dumps({
        "year": rtc[0],
        "month": rtc[1],
        "day": rtc[2],
        "hour": rtc[4],
        "minute": rtc[5],
        "second": rtc[6]
    }), {'Content-Type': 'application/json'}



async def fetch_ntp_time(timeout=2):
    import usocket
    import ustruct
    import uselect
    NTP_DELTA = 3155673600

    hosts = []
    try:
        gw = wifi.ifconfig()[2]
        if gw and gw != '0.0.0.0':
            hosts.append(gw)
    except Exception:
        pass
    hosts.extend(['162.159.200.1', '216.239.35.0'])

    for host in hosts:
        s = None
        poll = None
        try:
            if host.replace('.', '').isdigit():
                addr = (host, 123)
            else:
                addr = usocket.getaddrinfo(host, 123)[0][-1]
            s = usocket.socket(usocket.AF_INET, usocket.SOCK_DGRAM)
            s.setblocking(False)
            msg = bytearray(48)
            msg[0] = 0x1B
            s.sendto(msg, addr)
            poll = uselect.poll()
            poll.register(s, uselect.POLLIN)
            t_start = time.time()
            while time.time() - t_start < timeout:
                if poll.poll(0):
                    msg = s.recv(48)
                    val = ustruct.unpack('!I', msg[40:44])[0]
                    return val - NTP_DELTA
                await asyncio.sleep_ms(30)
        except Exception:
            pass
        finally:
            if poll and s:
                try:
                    poll.unregister(s)
                except Exception:
                    pass
            if s:
                try:
                    s.close()
                except Exception:
                    pass
    raise OSError(116, "ETIMEDOUT")

async def sync_time():
    await asyncio.sleep(8)  # Let server start before attempting NTP
    while True:
        try:
            t = await fetch_ntp_time(timeout=2)
            now_utc = time.localtime(t)
            base_offset = int(config.get("timezone", -7))
            year, month, day, hour, minute, second, weekday, yearday = now_utc
            dst_active = is_dst(year, month, day, weekday) if base_offset != 0 else False
            offset = base_offset + (1 if dst_active else 0)
            adjusted_time = time.localtime(t + offset * 3600)
            machine.RTC().datetime((adjusted_time[0], adjusted_time[1], adjusted_time[2],
                                    adjusted_time[6]+1, adjusted_time[3], adjusted_time[4], adjusted_time[5], 0))
            log_message(f"Time synchronized via NTP. Local time: {adjusted_time}")
            await asyncio.sleep(86400)  # once per day
        except Exception as e:
            log_message(f"Failed to sync time: {e}")
            await asyncio.sleep(60)  # retry in 1 minute on failure
        gc.collect()

async def check_schedules():
    global timers
    last_minute_check = -1

    def decrement_rain_delay():
        try:
            with open("rain_delay.json") as f:
                delay_data = ujson.load(f)
        except:
            delay_data = {"days_remaining": 0}

        remaining = max(0, int(delay_data.get("days_remaining", 0)) - 1)
        try:
            with open("rain_delay.json", "w") as f:
                ujson.dump({"days_remaining": remaining}, f)
        except Exception:
            pass
        log_message(f"Rain delay updated: {remaining} days remaining.")
        if MQTT == 1:
            try:
                client.publish("stat/rain_delay/state", str(remaining))
            except Exception:
                pass

    last_checked_day = None  # So we only decrement once per new day

    while True:
        try:
            now = time.localtime()
            current_hour = now[3]
            current_minute = now[4]
            current_time = f"{current_hour:02}:{current_minute:02}"
            current_day = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][now[6]]

            # 🌧 Decrement rain delay once per day
            today_date = now[2]
            if today_date != last_checked_day:
                decrement_rain_delay()
                last_checked_day = today_date

            rain_delay = get_rain_delay_days_remaining()

            # 🕒 Run schedule logic once per minute
            if current_minute != last_minute_check:
                last_minute_check = current_minute
                scheds_data = schedules

                try:
                    master_valve = int(config.get("master_valve", -1))
                except:
                    master_valve = -1

                for pin, zone_config in enumerate(scheds_data):
                    if pin == master_valve:
                        continue

                    if not zone_config.get('enabled', False):
                        continue

                    zone_schedules = zone_config.get('schedules', [])
                    for schedule in zone_schedules:
                        if not schedule.get('enabled', False):
                            continue

                        days = schedule.get('days', [])
                        on_time = schedule.get('onTime')
                        off_time = schedule.get('offTime')

                        if current_day in days:
                            if current_time == on_time:
                                if rain_delay > 0:
                                    log_message(f"Rain delay active — skipping schedules (days remaining: {rain_delay})")
                                else:
                                    on_hour, on_minute = map(int, on_time.split(':'))
                                    off_hour, off_minute = map(int, off_time.split(':'))

                                    # Convert to minutes since midnight
                                    on_total = on_hour * 60 + on_minute
                                    off_total = off_hour * 60 + off_minute

                                    # Handle overnight schedules
                                    duration_minutes = (off_total - on_total) % (24 * 60)
                                    set_relay_value(pin, 1)
                                    timers[pin] = {
                                        'end_time': time.time() + duration_minutes * 60,
                                        'task': None
                                    }
                                    log_message(f"Relay {pin+1} turned ON at {current_time} (schedule, {duration_minutes} min).")
                                    if MQTT == 1:
                                        publish_relay_status(client, pin, 1)
                
                            elif current_time == off_time:
                                if rain_delay > 0:
                                    log_message(f"Rain delay active — skipping schedules (days remaining: {rain_delay})")
                                else: 
                                    set_relay_value(pin, 0)
                                    if pin in timers:
                                        del timers[pin]
                                    log_message(f"Relay {pin+1} turned OFF at {current_time} (schedule).")
                                    if MQTT == 1:
                                        publish_relay_status(client, pin, 0)

            # ⏳ Timer expiration logic (manual or scheduled)
            current_epoch = time.time()
            expired = []

            for pin, data in list(timers.items()):
                if current_epoch >= data['end_time']:
                    expired.append(pin)

            for pin in expired:
                del timers[pin]
                set_relay_value(pin, 0)
                log_message(f"Zone {pin+1} timer expired. Relay turned OFF.")
                if MQTT == 1:
                    publish_relay_status(client, pin, 0)

            monitor_memory()

        except Exception as e:
            log_message(f"Exception in check_schedules: {e}")
            gc.collect()

        await asyncio.sleep(1)






async def run_queue_processor():
    global run_queue, current_queue_index, queue_active, current_zone_start_time
    while True:
        try:
            if queue_active and run_queue:
                if current_queue_index < len(run_queue):
                    item = run_queue[current_queue_index]
                    pin = item["pin"]
                    duration_secs = item["duration"]
                    
                    log_message(f"Run Queue: Starting Zone {pin+1} for {duration_secs} seconds.")
                    set_relay_value(pin, 1)
                    timers[pin] = {
                        'end_time': time.time() + duration_secs,
                        'task': None
                    }
                    if MQTT == 1:
                        try:
                            publish_relay_status(client, pin, 1)
                        except:
                            pass
                    
                    current_zone_start_time = time.time()
                    
                    elapsed = 0
                    while elapsed < duration_secs and queue_active:
                        await asyncio.sleep(1)
                        elapsed = int(time.time() - current_zone_start_time)
                    
                    # Turn off the zone
                    set_relay_value(pin, 0)
                    if pin in timers:
                        del timers[pin]
                    if MQTT == 1:
                        try:
                            publish_relay_status(client, pin, 0)
                        except:
                            pass
                    
                    if queue_active:
                        current_queue_index += 1
                    else:
                        log_message("Run Queue stopped manually.")
                else:
                    log_message("Run Queue finished successfully.")
                    queue_active = False
                    run_queue = []
                    current_queue_index = -1
            else:
                await asyncio.sleep(1)
        except Exception as e:
            log_message(f"Error in run_queue_processor: {e}")
            await asyncio.sleep(1)

def publish_relay_status(client, relay, status):
    global is_mqtt_connected
    if not is_mqtt_connected or client is None:
        return
    try:
        topic = f"stat/zone/{relay}/state"
        payload = "ON" if status else "OFF"
        client.publish(topic, payload)
        log_message(f"Published relay status for relay {relay}: {payload}")
    except Exception as e:
        log_message(f"Failed to publish relay status: {e}")
        is_mqtt_connected = False
        try:
            if getattr(client, 'sock', None):
                client.sock.close()
                client.sock = None
        except Exception:
            pass

async def ap_wifi_reconnect_loop():
    reconnect_attempts = 0
    while True:
        try:
            if SSID:
                wifi = network.WLAN(network.STA_IF)
                if not wifi.active():
                    wifi.active(True)
                if not wifi.isconnected():
                    reconnect_attempts += 1
                    if reconnect_attempts % 4 == 1:
                        log_message(f"Retrying Wi-Fi '{SSID}' from fallback AP mode ({reconnect_attempts})...")
                    if wifi.status() != 1001:
                        try:
                            _connect_sta(wifi, SSID, PASSWORD)
                        except Exception:
                            pass
                else:
                    ip = wifi.ifconfig()[0]
                    if ip and ip != '0.0.0.0':
                        log_message(f"Wi-Fi connected successfully! IP: {ip}. Exiting AP mode...")
                        try:
                            wifi.config(pm=0)
                        except Exception:
                            pass
                        try:
                            ap = network.WLAN(network.AP_IF)
                            if ap.active():
                                ap.active(False)
                        except Exception:
                            pass
                        # Start normal background services
                        if MQTT == 1 and client is not None:
                            asyncio.create_task(connect_mqtt())
                            asyncio.create_task(check_messages())
                            asyncio.create_task(mqtt_ping_loop())
                        asyncio.create_task(sync_time())
                        asyncio.create_task(check_schedules())
                        asyncio.create_task(wifi_monitor_loop())
                        asyncio.create_task(mdns_responder_task())
                        return
        except Exception as e:
            log_message(f"Error in ap_wifi_reconnect_loop: {e}")
        await asyncio.sleep(15)

async def enter_AP_mode():
    try:
        if SSID:
            try:
                sta = network.WLAN(network.STA_IF)
                if not sta.active():
                    sta.active(True)
            except Exception:
                pass

        ap_ssid = "intelidwellSC"
        ap_password = "Sprinkler12345"

        ap = network.WLAN(network.AP_IF)
        if not ap.active():
            ap.active(True)
        try:
            ap.config(essid=ap_ssid, password=ap_password)
        except Exception:
            pass
        try:
            ap.ifconfig(('192.168.4.1', '255.255.255.0', '192.168.4.1', '192.168.4.1'))
        except Exception:
            pass

        log_message(f"Configuration mode activated. Connect to AP: {ap_ssid} with password:{ap_password}")
        log_message("Visit http://192.168.4.1 in your web browser to configure.")

        # Ensure web server is running
        if not server_running:
            asyncio.create_task(run_server())

        if SSID:
            asyncio.create_task(ap_wifi_reconnect_loop())

        while True:
            await asyncio.sleep(3600)
    except Exception as e:
        log_message(f"Error in enter_AP_mode: {e}")

def publish_schedule_status(client, pin, enabled):
    try:
        topic = f"stat/zone/{pin}/schedule"
        payload = "true" if enabled else "false"
        client.publish(topic, payload)
        log_message(f"Published schedule status for relay {pin}: {payload}")
    except Exception as e:
        log_message(f"Failed to publish schedule status: {e}")

server_running = False

async def run_server():
    global server_running
    if server_running:
        return
    server_running = True
    bind_failures = 0
    try:
        while True:
            gc.collect()
            log_message("Starting Microdot server...")
            try:
                await app.start_server(host='0.0.0.0', port=80)
            except Exception as e:
                bind_failures += 1
                log_message(f"Microdot server crashed: {e}. Restarting in 5 seconds...")
                try:
                    app.shutdown()
                except:
                    pass
                if bind_failures >= 3:
                    log_message("Microdot socket unrecoverable. Rebooting ESP32...")
                    await asyncio.sleep(1)
                    import machine
                    machine.reset()
                await asyncio.sleep(5)
    finally:
        server_running = False

async def _probe_tcp_reachable(host, port=80, timeout_ms=1200):
    import usocket
    import uselect
    sock = None
    poll = None
    try:
        sock = usocket.socket(usocket.AF_INET, usocket.SOCK_STREAM)
        sock.setblocking(False)
        try:
            sock.connect((host, port))
            return True
        except OSError as e:
            err_code = e.args[0] if e.args else 0
            # EINPROGRESS (119 / 115) -> poll for SYN-ACK or RST
            if err_code not in (115, 119):
                return err_code == 111  # ECONNREFUSED proves host replied over LAN
        poll = uselect.poll()
        poll.register(sock, uselect.POLLOUT | uselect.POLLERR | uselect.POLLHUP)
        steps = max(1, timeout_ms // 50)
        for _ in range(steps):
            ev = poll.poll(0)
            if ev:
                for _, flag in ev:
                    if (flag & uselect.POLLOUT) and not (flag & (uselect.POLLERR | uselect.POLLHUP)):
                        return True
                    try:
                        sock.send(b'')
                        return True
                    except OSError as e:
                        if e.args and e.args[0] in (111, 104):
                            return True
                    return False
            await asyncio.sleep_ms(50)
        return False
    except Exception:
        return False
    finally:
        if poll is not None and sock is not None:
            try:
                poll.unregister(sock)
            except Exception:
                pass
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

async def wifi_monitor_loop():
    await asyncio.sleep(20)  # Initial grace period for Wi-Fi and DHCP
    consecutive_failures = 0

    while True:
        try:
            wifi = network.WLAN(network.STA_IF)
            is_healthy = False
            if wifi.isconnected() and wifi.ifconfig()[0] != '0.0.0.0':
                try:
                    wifi.config(pm=0)
                except Exception:
                    pass
                # Active Cloud WebSocket proves Wi-Fi/TCP health without extra probes
                if 'cloud_client' in globals() and cloud_client and cloud_client.is_connected():
                    last_rx = getattr(cloud_client, '_last_rx_ts', 0)
                    if time.time() - last_rx < 65:
                        is_healthy = True
                if not is_healthy:
                    gw = wifi.ifconfig()[2]
                    if gw and gw != '0.0.0.0':
                        is_healthy = await _probe_tcp_reachable(gw, 80, 1200)
                        if not is_healthy and wifi.ifconfig()[0].startswith('192.168.1.'):
                            is_healthy = await _probe_tcp_reachable('192.168.1.74', 80, 1200)
                    else:
                        is_healthy = True

            if is_healthy:
                if consecutive_failures > 0:
                    log_message(f"Wi-Fi connection restored! IP: {wifi.ifconfig()[0]}")
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                if consecutive_failures % 3 == 1:
                    log_message(f"Wi-Fi / Gateway unreachable ({consecutive_failures * 10}s). Checking link...")

                # If TCP link is unresponsive for 30s/90s/180s, force re-association
                if consecutive_failures in (3, 9, 18):
                    log_message("Wi-Fi link unresponsive. Forcing re-association...")
                    try:
                        wifi.disconnect()
                        await asyncio.sleep(2)
                        if SSID:
                            _connect_sta(wifi, SSID, PASSWORD)
                    except Exception as e:
                        log_message(f"Re-association failed: {e}")
                elif not wifi.isconnected():
                    if wifi.status() != 1001:  # 1001 = STAT_CONNECTING
                        try:
                            if SSID:
                                _connect_sta(wifi, SSID, PASSWORD)
                        except Exception:
                            pass

                # If connection has been down for > 5 minutes (30 checks), reboot cleanly to restore network stack
                if consecutive_failures >= 30:
                    log_message("Wi-Fi unreachable for >5m! Rebooting device to recover network stack...")
                    await asyncio.sleep(1)
                    import machine
                    machine.reset()
        except Exception as e:
            log_message(f"Error in wifi_monitor_loop: {e}")
        await asyncio.sleep(10)

async def mdns_responder_task():
    await asyncio.sleep(2)
    sock = None
    try:
        import usocket as socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setblocking(False)
        sock.bind(('', 5353))
        mreq = bytes([224, 0, 0, 251, 0, 0, 0, 0])
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except Exception:
            pass
        log_message("mDNS: Responder active on port 5353 for sprinklers.local")
    except Exception as e:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        return

    buf = bytearray(256)
    try:
        wifi = network.WLAN(network.STA_IF)
        if wifi.isconnected() and wifi.ifconfig()[0] != '0.0.0.0':
            ip_str = wifi.ifconfig()[0]
            ip_bytes = bytes([int(x) for x in ip_str.split('.')])
            announcement = (
                b'\x00\x00\x84\x00\x00\x00\x00\x01\x00\x00\x00\x00' +
                b'\x0asprinklers\x05local\x00' +
                b'\x00\x01\x80\x01\x00\x00\x00\x78\x00\x04' +
                ip_bytes
            )
            sock.sendto(announcement, ('224.0.0.251', 5353))
    except Exception:
        pass

    while True:
        try:
            nbytes, addr = sock.recvfrom_into(buf)
            if nbytes and nbytes > 12:
                data_slice = memoryview(buf)[:nbytes]
                if b'sprinklers' in data_slice:
                    wifi = network.WLAN(network.STA_IF)
                    ip_str = wifi.ifconfig()[0]
                    if ip_str and ip_str != '0.0.0.0':
                        ip_bytes = bytes([int(x) for x in ip_str.split('.')])
                        tx_id = bytes(data_slice[:2])
                        body_tail = (
                            b'\x84\x00\x00\x00\x00\x01\x00\x00\x00\x00' +
                            b'\x0asprinklers\x05local\x00' +
                            b'\x00\x01\x80\x01\x00\x00\x00\x78\x00\x04' +
                            ip_bytes
                        )
                        try:
                            sock.sendto(b'\x00\x00' + body_tail, ('224.0.0.251', 5353))
                        except Exception:
                            pass
                        if addr and addr[0] != '224.0.0.251':
                            try:
                                sock.sendto(tx_id + body_tail, addr)
                            except Exception:
                                pass
        except Exception:
            pass
        await asyncio.sleep_ms(100)

def handle_async_exception(loop, context):
    try:
        exc = context.get('exception')
        msg = context.get('message', 'Async loop task error')
        log_message(f"Async Loop Error ({msg}): {exc}")
    except:
        pass

async def main():
    try:
        loop = asyncio.get_event_loop()
        loop.set_exception_handler(handle_async_exception)
    except Exception:
        pass

    if EARLY_AP_MODE:
        global MQTT
        MQTT = 0
        log_message("Early AP mode active, running bare web server.")
        asyncio.create_task(run_queue_processor())
        await enter_AP_mode()
        return

    wifi_ok = await connect_to_wifi(attempts=3, timeout_per_attempt=15)
    if not wifi_ok:
        log_message("Failed to connect to Wi-Fi. Entering fallback AP mode while retrying...")
        await enter_AP_mode()
        return

    try:
        if not server_running:
            asyncio.create_task(run_server())
        if MQTT == 1 and client is not None:
            asyncio.create_task(connect_mqtt())
            asyncio.create_task(check_messages())
            asyncio.create_task(mqtt_ping_loop())
        if cloud_client and cloud_client.is_enabled():
            asyncio.create_task(cloud_client.loop())
        asyncio.create_task(sync_time())
        asyncio.create_task(check_schedules())
        asyncio.create_task(run_queue_processor())
        asyncio.create_task(wifi_monitor_loop())
        asyncio.create_task(mdns_responder_task())
        while True:
            await asyncio.sleep(3600)
    except Exception as e:
        log_message(f"Error in main loop: {e}")
        while True:
            await asyncio.sleep(3600)

def start():
    global server_running
    while True:
        try:
            server_running = False
            gc.collect()
            asyncio.run(main())
        except Exception as e:
            server_running = False
            log_message(f"Fatal error in main loop: {e}. Rebooting in 3s...")
            try:
                time.sleep(3)
            except Exception:
                pass
            import machine
            machine.reset()

if __name__ == '__main__':
    start()