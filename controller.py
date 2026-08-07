# Intellidwell Sprinkler Controller Firmware
# Copyright (C) 2025 Tanner Nelson
#
# Licensed under GPLv3 with additional non-commercial hardware restrictions.
# See LICENSE for details.
import gc
gc.collect()  # Collect immediately to start with a clean heap

FIRMWARE_VERSION = "0.0.0-dev"

import network

from microdot_asyncio import Microdot, send_file
import machine
from machine import Pin, RTC, reset
import ujson
import utime as time
import uasyncio as asyncio
from umqtt.simple import MQTTClient
import ntptime
import uos as os

machine.sleep(0)       # Disable light sleep
global MQTT

timers = {}
run_queue = []
current_queue_index = -1
queue_active = False
current_zone_start_time = 0


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
        "timezone": config.get("timezone", "-7"),
        "master_valve": int(config.get("master_valve", -1)),
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
        master_valve = int(config.get("master_valve", -1))
        
        # If we have a master valve configured and this pin is not the master valve
        if master_valve >= 0 and master_valve < len(relays) and pin != master_valve:
            # Determine if any OTHER zone is already on (excluding this pin, since we haven't set it yet)
            any_other_relay_on = False
            for idx in range(len(relays)):
                if idx != master_valve and idx != pin:
                    if relays[idx].value() == 1:
                        any_other_relay_on = True
                        break
            
            # If we are turning this zone ON
            if value == 1:
                # Master valve must turn on. Is it currently off?
                if relays[master_valve].value() == 0:
                    # Turn on master valve first!
                    relays[master_valve].value(1)
                    log_message(f"Master valve (Relay {master_valve+1}) set to 1 (pre-delay)")
                    if MQTT == 1:
                        try:
                            publish_relay_status(client, master_valve, 1)
                        except Exception:
                            pass
                    # Wait 1000ms before turning on the zone valve to prevent brownout
                    time.sleep_ms(1000)
            
            # Now set the zone valve
            relays[pin].value(value)
            
            # Re-evaluate expected master valve state in case we turned something off
            # (or if we turned a zone on, we want to ensure everything is correct)
            any_relay_on = False
            for idx in range(len(relays)):
                if idx != master_valve:
                    if relays[idx].value() == 1:
                        any_relay_on = True
                        break
            expected_master_state = 1 if any_relay_on else 0
            
            # If we need to update the master valve state (e.g. turning it OFF)
            if relays[master_valve].value() != expected_master_state:
                relays[master_valve].value(expected_master_state)
                log_message(f"Master valve (Relay {master_valve+1}) set to {expected_master_state}")
                if MQTT == 1:
                    try:
                        publish_relay_status(client, master_valve, expected_master_state)
                    except Exception:
                        pass
        else:
            # No master valve configured, or we are setting the master valve pin directly
            relays[pin].value(value)


def stop_all_watering():
    global timers, queue_active, run_queue, current_queue_index
    queue_active = False
    run_queue = []
    current_queue_index = -1
    timers.clear()
    for idx, relay in enumerate(relays):
        set_relay_value(idx, 0)
        if MQTT == 1:
            try:
                publish_relay_status(client, idx, 0)
            except:
                pass
    log_message("All watering stopped.")


CLIENT_ID = "intellidwell_SC"
TOPIC_BASE = "home/sprinklers/"

client = MQTTClient(CLIENT_ID, MQTT_BROKER, user=MQTT_USER, password=MQTT_PASSWORD, keepalive=60)
client.set_last_will(f"{TOPIC_BASE}status", "Offline", retain=True)

LOG_FILE = 'logs.txt'
LOG_MAX_LINES = 25  # Keep only the last 25 lines of logs
MAX_RECONNECT_ATTEMPTS = 10  # Maximum number of reconnection attempts

#WIFI and MQTT Credentials

def is_dst(year, month, day, weekday):
    """Determine if current date falls in US DST range."""
    # Compute second Sunday in March
    if month == 3:
        second_sunday = 14 - (time.mktime((year, 3, 1, 0, 0, 0, 0, 0)) % 7)
        return day >= second_sunday

    # Compute first Sunday in November
    if month == 11:
        first_sunday = 7 - (time.mktime((year, 11, 1, 0, 0, 0, 0, 0)) % 7)
        return day < first_sunday

    return 3 < month < 11  # April–October

# Settings loader is unified above

def save_settings(settings):
    global config
    if settings is None or not isinstance(settings, dict):
        raise ValueError("Invalid settings format. Expected a dictionary.")
    try:
        settings['mqtt_enabled'] = int(settings.get('mqtt_enabled', 0))  
        settings['master_valve'] = int(settings.get('master_valve', -1))
        with open('settings.json', 'w') as f:
            ujson.dump(settings, f)
        config = load_settings()
        log_message("Settings saved successfully.")
    except Exception as e:
        log_message(f"Error saving settings: {e}")




_log_cache = []
try:
    with open(LOG_FILE, 'r') as f:
        _log_cache = f.read().splitlines()
    if len(_log_cache) > LOG_MAX_LINES:
        _log_cache = _log_cache[-LOG_MAX_LINES:]
except Exception:
    _log_cache = []

def log_message(message):
    global _log_cache
    current_time = time.localtime()
    timestamp = "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(*current_time[:6])
    log_entry = f"{timestamp}: {message}"
    
    _log_cache.append(log_entry)
    if len(_log_cache) > LOG_MAX_LINES:
        _log_cache = _log_cache[-LOG_MAX_LINES:]
        
    try:
        with open(LOG_FILE, 'w') as f:
            f.write("\n".join(_log_cache) + "\n")
    except Exception as e:
        try:
            print(f"Failed to log message: {e}")
            print(f"LOG ENTRY (Fallback): {log_entry}")
        except OSError:
            pass

    try:
        print(f"LOG: {timestamp}: {message}")
    except OSError:
        pass


def load_rain_delay():
    try:
        with open("rain_delay.json") as f:
            return int(ujson.load(f).get("days_remaining", 0))
    except Exception:
        return 0

rain_delay_days = load_rain_delay()

def get_rain_delay_days_remaining():
    global rain_delay_days
    return rain_delay_days

def set_rain_delay_days(days):
    global rain_delay_days
    try:
        with open("rain_delay.json", "w") as f:
            ujson.dump({"days_remaining": days}, f)
        rain_delay_days = days
        log_message(f"Rain delay set to {days} days.")
        if MQTT == 1:
            try:
                client.publish("stat/rain_delay/state", str(days))
            except Exception:
                pass
    except Exception as e:
        log_message(f"Failed to set rain delay: {e}")

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
        publish_relay_status(client, pin, relays[pin].value())
    elif len(parts) == 4 and parts[1] == "zone" and parts[3] == "schedule":
        pin = int(parts[2])
        status = msg.lower() == "true"
        if update_schedule_status(pin, status):
            publish_schedule_status(client, pin, status)
        else:
            log_message("Invalid schedule operation")
    elif topic == "cmnd/rain_delay/set":
        try:
            days = int(msg)
            set_rain_delay_days(days)
        except Exception as e:
            log_message(f"Failed to set rain delay via MQTT: {e}")
    elif topic == "cmnd/restart":
        if msg == "RESTART":
            log_message("Restart command received via MQTT. Rebooting...")
            reset()

client.set_callback(command_callback)

async def connect_mqtt():
    global is_mqtt_connected
    try:
        client.connect()
        is_mqtt_connected = True
        log_message("MQTT connected.")
        await subscribe_to_topics()
    except Exception as e:
        is_mqtt_connected = False
        log_message(f"Failed to connect to MQTT at startup: {e}")

def publish_rain_delay_discovery(client):
    try:
        topic = "homeassistant/number/rain_delay/config"
        payload = {
            "name": "Rain Delay",
            "command_topic": "cmnd/rain_delay/set",
            "state_topic": "stat/rain_delay/state",
            "min": 0,
            "max": 5,
            "step": 1,
            "unique_id": "intellidwell_rain_delay",
            "device": {
                "identifiers": ["intellidwellSC"],
                "name": "Sprinkler Controller",
                "manufacturer": "Intellidwell",
                "model": "Sprinkler Controller V1.0",
                "sw_version": "1.0"
            },
            "availability_topic": f"{TOPIC_BASE}status",
            "payload_available": "Online",
            "payload_not_available": "Offline",
            "platform": "mqtt"
        }
        client.publish(topic, ujson.dumps(payload), retain=True)
        log_message("Published MQTT rain delay discovery message")
    except Exception as e:
        log_message(f"Failed to publish rain delay discovery: {e}")

def publish_restart_discovery(client):
    try:
        topic = "homeassistant/button/intellidwell_restart/config"
        payload = {
            "name": "Restart",
            "command_topic": "cmnd/restart",
            "payload_press": "RESTART",
            "unique_id": "intellidwell_restart_btn",
            "device": {
                "identifiers": ["intellidwellSC"],
                "name": "Sprinkler Controller",
                "manufacturer": "Intellidwell",
                "model": "Sprinkler Controller V1.0",
                "sw_version": "1.0"
            },
            "availability_topic": f"{TOPIC_BASE}status",
            "payload_available": "Online",
            "payload_not_available": "Offline",
            "platform": "mqtt"
        }
        client.publish(topic, ujson.dumps(payload), retain=True)
        log_message("Published MQTT restart button discovery message")
    except Exception as e:
        log_message(f"Failed to publish restart discovery: {e}")

async def subscribe_to_topics():
    try:
        # Publish Online status first
        try:
            client.publish(f"{TOPIC_BASE}status", "Online", retain=True)
            log_message("Published Online status to MQTT")
        except Exception as e:
            log_message(f"Failed to publish Online status: {e}")

        for i in range(len(relays)):
            client.subscribe(f"cmnd/zone/{i}/power")
            client.subscribe(f"cmnd/zone/{i}/schedule")
            log_message(f"Subscribed to: cmnd/zone/{i}/power and cmnd/zone/{i}/schedule")
        client.subscribe("cmnd/rain_delay/set")
        client.subscribe("cmnd/restart")
        log_message("Subscribed to: cmnd/rain_delay/set and cmnd/restart")
        publish_discovery(client)
        publish_schedule_discovery(client)
        publish_rain_delay_discovery(client)
        publish_restart_discovery(client)
        try:
            client.publish("stat/rain_delay/state", str(get_rain_delay_days_remaining()))
        except:
            pass
        # Publish current states so Home Assistant has them immediately
        for i in range(len(relays)):
            try:
                publish_relay_status(client, i, relays[i].value())
            except:
                pass
            try:
                publish_schedule_status(client, i, schedules[i].get("enabled", True))
            except:
                pass
    except Exception as e:
        log_message(f"Failed to subscribe to topics: {e}")

async def check_messages():
    global is_mqtt_connected
    while True:
        try:
            if MQTT == 1:
                if not is_mqtt_connected:
                    log_message("MQTT not connected. Attempting connection...")
                    try:
                        try:
                            client.disconnect()
                        except:
                            pass
                        await asyncio.sleep(2)
                        client.connect()
                        is_mqtt_connected = True
                        log_message("MQTT connected.")
                        await subscribe_to_topics()
                    except Exception as e:
                        is_mqtt_connected = False
                        log_message(f"Failed to connect to MQTT: {e}")
                        await asyncio.sleep(15)
                        continue
                
                try:
                    client.check_msg()
                except Exception as e:
                    log_message(f"MQTT connection lost or check_msg failed: {e}")
                    is_mqtt_connected = False
                    try:
                        client.disconnect()
                    except:
                        pass
        except Exception as e:
            log_message(f"Error in check_messages: {e}")
            is_mqtt_connected = False
            
        await asyncio.sleep(1)
        gc.collect()  # Run garbage collection to free up memory

# New helper to monitor memory usage
def monitor_memory():
    free_memory = gc.mem_free()
    if free_memory < 8000:
        gc.collect()
        free_memory = gc.mem_free()
        if free_memory < 6000:
            log_message(f"Low memory warning! Free: {free_memory} bytes")

# Web server app
app = Microdot()

@app.after_request
def cleanup(request, response):
    gc.collect()
    return response

@app.route('/logs-page')
def logs_page(request):
    gc.collect()
    return send_file('logs.html')

@app.route('/get-logs')
def get_logs(request):
    try:
        with open(LOG_FILE, 'r') as f:
            logs = f.read()
        return logs, {'Content-Type': 'text/plain'}
    except OSError as e:
        log_message(f"Error reading logs: {e}")
        return 'Error reading logs', 500
    except Exception as e:
        log_message(f"Unexpected error: {e}")
        return 'Unexpected error', 500


@app.route('/clear-logs', methods=['POST'])
def clear_logs(request):
    try:
        with open(LOG_FILE, 'w') as f:
            f.write("")
        log_message("Logs cleared.")
        return 'Logs cleared', 200
    except Exception as e:
        log_message(f"Error clearing logs: {e}")
        return 'Error clearing logs', 500

@app.route('/restart', methods=['POST', 'OPTIONS'])
async def restart(request):
    if request.method == 'OPTIONS':
        return '', 200, {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'POST, OPTIONS',
            'Access-Control-Allow-Private-Network': 'true'
        }
    log_message("Restarting device in 1 second...")
    async def do_reset():
        await asyncio.sleep(1)
        reset()
    asyncio.create_task(do_reset())
    return 'Restarting...', 200, {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Private-Network': 'true'
    }


@app.route('/')
def index(request):
    gc.collect()
    return send_file('index.html')

@app.route('/settings')
def settings(request):
    gc.collect()
    return send_file('settings.html')

@app.route('/get-settings', methods=['GET', 'OPTIONS'])
def get_settings(request):
    if request.method == 'OPTIONS':
        return '', 200, {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, OPTIONS',
            'Access-Control-Allow-Private-Network': 'true'
        }
    response_data = dict(config)
    response_data["firmware_version"] = FIRMWARE_VERSION
    return ujson.dumps(response_data), 200, {
        'Content-Type': 'application/json',
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Private-Network': 'true'
    }

@app.route('/edit-schedule/<pin>/<sched_id>', methods=['POST', 'OPTIONS'])
def edit_schedule(request, pin, sched_id):
    if request.method == 'OPTIONS':
        return '', 200, {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'POST, OPTIONS'
        }
    try:
        pin = int(pin)
        if not (0 <= pin < len(relays)):
            return 'Invalid pin', 400, {'Access-Control-Allow-Origin': '*'}
        cfg = config
        if pin == int(cfg.get("master_valve", -1)):
            return 'Cannot schedule master valve zone', 400, {'Access-Control-Allow-Origin': '*'}
        data = request.json
        if data is None:
            return 'Invalid data', 400, {'Access-Control-Allow-Origin': '*'}
        days = data.get('days', [])
        on_time = data.get('onTime')
        off_time = data.get('offTime')
        if not days or not on_time or not off_time:
            return 'Missing required fields', 400, {'Access-Control-Allow-Origin': '*'}
        
        found = False
        for s in schedules[pin]['schedules']:
            if s.get('id') == sched_id:
                s['days'] = days
                s['onTime'] = on_time
                s['offTime'] = off_time
                found = True
                break
        
        if found:
            save_schedules(schedules)
            log_message(f"Edited schedule {sched_id} on Zone {pin+1}")
            return 'Updated', 200, {'Access-Control-Allow-Origin': '*'}
        return 'Schedule not found', 404, {'Access-Control-Allow-Origin': '*'}
    except Exception as e:
        log_message(f"Error editing schedule: {e}")
        return 'Failed to edit schedule', 500, {'Access-Control-Allow-Origin': '*'}

@app.route('/start-run-queue', methods=['POST', 'OPTIONS'])
def start_run_queue(request):
    if request.method == 'OPTIONS':
        return '', 200, {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'POST, OPTIONS'
        }
    global run_queue, current_queue_index, queue_active
    try:
        data = request.json
        if not data or 'queue' not in data:
            return 'Invalid data', 400, {'Access-Control-Allow-Origin': '*'}
        
        stop_all_watering()
        
        run_queue = []
        for item in data['queue']:
            pin = int(item['pin'])
            mins = int(item['duration_mins'])
            if 0 <= pin < len(relays) and mins > 0:
                run_queue.append({"pin": pin, "duration": mins * 60})
        
        if run_queue:
            queue_active = True
            current_queue_index = 0
            log_message(f"Starting sequential runs for {len(run_queue)} zones.")
            return 'Queue started', 200, {'Access-Control-Allow-Origin': '*'}
        return 'Queue is empty', 400, {'Access-Control-Allow-Origin': '*'}
    except Exception as e:
        log_message(f"Error starting run queue: {e}")
        return 'Error', 500, {'Access-Control-Allow-Origin': '*'}

@app.route('/stop-run-queue', methods=['POST', 'OPTIONS'])
def stop_run_queue_route(request):
    if request.method == 'OPTIONS':
        return '', 200, {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'POST, OPTIONS'
        }
    stop_all_watering()
    return 'Queue stopped', 200, {'Access-Control-Allow-Origin': '*'}

@app.route('/get-run-queue-status', methods=['GET', 'OPTIONS'])
def get_run_queue_status(request):
    if request.method == 'OPTIONS':
        return '', 200, {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, OPTIONS'
        }
    global run_queue, current_queue_index, queue_active, current_zone_start_time
    remaining = 0
    if queue_active and 0 <= current_queue_index < len(run_queue):
        elapsed = time.time() - current_zone_start_time
        item = run_queue[current_queue_index]
        remaining = max(0, int(item['duration'] - elapsed))
        
    formatted_queue = []
    for item in run_queue:
        formatted_queue.append({
            "pin": item["pin"],
            "duration_mins": item["duration"] // 60
        })
        
    return ujson.dumps({
        "active": queue_active,
        "current_index": current_queue_index,
        "remaining_seconds": remaining,
        "queue": formatted_queue
    }), 200, {
        'Content-Type': 'application/json',
        'Access-Control-Allow-Origin': '*'
    }

@app.route('/ota/upload', methods=['POST', 'OPTIONS'])
def ota_upload(request):
    if request.method == 'OPTIONS':
        return '', 200, {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'POST, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type, X-Filename',
            'Access-Control-Allow-Private-Network': 'true'
        }
    
    import gc
    gc.collect()
    
    filename = request.headers.get('X-Filename')
    if not filename:
        return 'Missing X-Filename header', 400, {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Private-Network': 'true'
        }
        
    try:
        # Clean up filename to prevent path traversal
        filename = filename.split('/')[-1].split('\\')[-1]
        tmp_filename = filename + '.tmp'
        
        # Write the uploaded bytes to a staging file
        with open(tmp_filename, 'wb') as f:
            f.write(request.body)
            
        # Rename staging file to final filename
        import os
        try:
            os.remove(filename)
        except OSError:
            pass
        os.rename(tmp_filename, filename)
        
        log_message(f"OTA: Uploaded and activated file {filename} ({len(request.body)} bytes)")
        return 'File uploaded successfully', 200, {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Private-Network': 'true'
        }
    except Exception as e:
        log_message(f"OTA: Failed to upload file {filename}: {e}")
        try:
            import os
            os.remove(tmp_filename)
        except:
            pass
        return f'Error: {e}', 500, {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Private-Network': 'true'
        }

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
            
        if version in ('stable', 'beta'):
            BASE_URL = f"{update_server}/static/sprinkler_src"
        elif version == 'deprecated':
            BASE_URL = f"{update_server}/static/sprinkler_src_deprecated"
        else:
            if version.startswith('2.'):
                BASE_URL = f"{update_server}/static/firmware/stable/{version}"
            else:
                BASE_URL = f"{update_server}/static/firmware/deprecated/{version}"
            
        files = ["controller.mpy", "main.py", "index.html", "logs.html", "scheduler.html", "settings.html"]
        total_files = len(files)
        
        for index, filename in enumerate(files):
            ota_logs.append(f"Downloading {filename}...")
            ota_progress = 10 + int((index / total_files) * 80)
            await asyncio.sleep(0.1)  # Cooperatively yield control
            
            res = urequests.get(f"{BASE_URL}/{filename}", stream=True)
            if res.status_code == 200:
                tmp_filename = filename + '.tmp'
                with open(tmp_filename, 'wb') as f:
                    while True:
                        chunk = res.raw.read(512)
                        if not chunk:
                            break
                        f.write(chunk)
                res.close()
                
                # Activate file
                try:
                    os.remove(filename)
                except OSError:
                    pass
                os.rename(tmp_filename, filename)
                ota_logs.append(f"Successfully updated {filename}.")
                ota_progress = 10 + int(((index + 1) / total_files) * 80)
            else:
                res.close()
                raise Exception(f"HTTP Status {res.status_code}")
                
            gc.collect()
            await asyncio.sleep(0.1)  # Cooperatively yield control
            
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
    if request.method == 'OPTIONS':
        return '', 200, {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'POST, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type',
            'Access-Control-Allow-Private-Network': 'true'
        }
    
    global ota_progress
    if ota_progress >= 0 and ota_progress < 100:
        return 'Update already in progress', 400, {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Private-Network': 'true'
        }
        
    version = request.args.get('version', 'stable') if request.args else 'stable'
    
    # Start the async update task
    asyncio.create_task(run_ota_update_task(version))
    
    return ujson.dumps({'status': 'started'}), 200, {
        'Content-Type': 'application/json',
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Private-Network': 'true'
    }

@app.route('/ota/status', methods=['GET', 'OPTIONS'])
def ota_status(request):
    if request.method == 'OPTIONS':
        return '', 200, {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type',
            'Access-Control-Allow-Private-Network': 'true'
        }
        
    global ota_progress, ota_logs, ota_complete, ota_error
    return ujson.dumps({
        'progress': ota_progress,
        'logs': ota_logs,
        'complete': ota_complete,
        'error': ota_error
    }), 200, {
        'Content-Type': 'application/json',
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Private-Network': 'true'
    }


@app.route('/save-settings', methods=['POST'])
def save_settings_route(request):
    settings = request.json
    if settings is None:
        return 'Invalid settings data', 400  # Respond with an error if no data is received
    try:
        save_settings(settings)
        return 'Settings saved successfully', 200
    except ValueError as e:
        log_message(f"Error saving settings: {e}")
        return 'Failed to save settings', 500


@app.route('/get-relay-states')
def get_relay_states(request):
    states = [relay.value() for relay in relays]
    return ujson.dumps(states), {'Content-Type': 'application/json'}

@app.route('/scheduler')
def scheduler(request):
    gc.collect()
    return send_file('scheduler.html')

@app.route('/set-schedule/<pin>', methods=['POST'])
def set_schedule(request, pin):
    try:
        pin = int(pin)
        cfg = config
        if pin == int(cfg.get("master_valve", -1)):
            return 'Cannot schedule master valve zone', 400
        days = request.form.getlist('day')
        onTime = request.form.get('onTime')
        offTime = request.form.get('offTime')
        import random
        sched_id = f"sch_{int(time.time())}_{random.randint(1000, 9999)}"
        new_schedule = {
            'id': sched_id,
            'days': days,
            'onTime': onTime,
            'offTime': offTime,
            'enabled' : True
        }
        schedules[pin]['schedules'].append(new_schedule)
        save_schedules(schedules)
        log_message(f"Schedule set for Relay {pin + 1}")
        return f'Schedule set for Relay {pin + 1}!'
    except Exception as e:
        log_message(f"Error setting schedule: {e}")
        return 'Failed to set schedule', 500

@app.route('/add-schedule/<pin>', methods=['POST'])
def add_schedule(request, pin):
    try:
        pin = int(pin)
        if not (0 <= pin < len(relays)):
            return 'Invalid pin', 400
        cfg = config
        if pin == int(cfg.get("master_valve", -1)):
            return 'Cannot schedule master valve zone', 400
        data = request.json
        if data is None:
            return 'Invalid data', 400
        days = data.get('days', [])
        on_time = data.get('onTime')
        off_time = data.get('offTime')
        if not days or not on_time or not off_time:
            return 'Missing required fields', 400
        import random
        sched_id = f"sch_{int(time.time())}_{random.randint(1000, 9999)}"
        new_schedule = {
            'id': sched_id,
            'days': days,
            'onTime': on_time,
            'offTime': off_time,
            'enabled': True
        }
        schedules[pin]['schedules'].append(new_schedule)
        save_schedules(schedules)
        log_message(f"Added schedule {sched_id} to Zone {pin+1}")
        return ujson.dumps(new_schedule), 200, {'Content-Type': 'application/json'}
    except Exception as e:
        log_message(f"Error adding schedule: {e}")
        return 'Failed to add schedule', 500

@app.route('/delete-schedule/<pin>/<sched_id>', methods=['POST'])
def delete_schedule(request, pin, sched_id):
    try:
        pin = int(pin)
        if not (0 <= pin < len(relays)):
            return 'Invalid pin', 400
        original_len = len(schedules[pin]['schedules'])
        schedules[pin]['schedules'] = [s for s in schedules[pin]['schedules'] if s.get('id') != sched_id]
        if len(schedules[pin]['schedules']) < original_len:
            save_schedules(schedules)
            log_message(f"Deleted schedule {sched_id} from Zone {pin+1}")
            return 'Deleted', 200
        return 'Schedule not found', 404
    except Exception as e:
        log_message(f"Error deleting schedule: {e}")
        return 'Failed to delete schedule', 500

@app.route('/toggle-schedule-item/<pin>/<sched_id>/<state>', methods=['POST'])
def toggle_schedule_item(request, pin, sched_id, state):
    try:
        pin = int(pin)
        if not (0 <= pin < len(relays)):
            return 'Invalid pin', 400
        enabled = state.lower() == 'true'
        found = False
        for s in schedules[pin]['schedules']:
            if s.get('id') == sched_id:
                s['enabled'] = enabled
                found = True
                break
        if found:
            save_schedules(schedules)
            log_message(f"Schedule {sched_id} on Zone {pin+1} set to {enabled}")
            return 'Toggled', 200
        return 'Schedule not found', 404
    except Exception as e:
        log_message(f"Error toggling schedule item: {e}")
        return 'Failed to toggle schedule item', 500

@app.route('/toggle-schedule/<pin>/<status>', methods=['GET'])
def toggle_schedule(request, pin, status):
    pin = int(pin)
    status = status.lower() == 'true'
    if 0 <= pin < len(schedules):
        schedules[pin]['enabled'] = status
        save_schedules(schedules)
        publish_schedule_status(client, pin, status)
        log_message(f"Schedule for Relay {pin+1} {'enabled' if status else 'disabled'}!")
        return f"Schedule for Relay {pin+1} {'enabled' if status else 'disabled'}!"
    return 'Invalid pin or status', 400

@app.route('/relay-timer/<pin>/<minutes>', methods=['GET', 'OPTIONS'])
def start_timer(request, pin, minutes):
    if request.method == 'OPTIONS':
        return '', 200, {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, OPTIONS'
        }
    try:
        pin = int(pin)
        minutes = int(minutes)

        if 0 <= pin < len(relays) and minutes > 0:
            stop_all_watering()
            set_relay_value(pin, 1)
            timers[pin] = {
                'end_time': time.time() + minutes * 60
            }
            log_message(f"Zone {pin + 1} turned ON (timer set for {minutes} minutes).")
            if MQTT == 1:
                publish_relay_status(client, pin, 1)
            return 'Timer started', 200, {'Access-Control-Allow-Origin': '*'}

        return 'Invalid input', 400, {'Access-Control-Allow-Origin': '*'}
    except Exception as e:
        log_message(f"Error in /relay-timer route: {e}")
        return 'Failed to start timer', 500, {'Access-Control-Allow-Origin': '*'}

@app.route('/timer-status')
def timer_status(request):
    try:
        now = time.time()
        status = {}

        expired = []

        for pin, data in list(timers.items()):  # safe iteration over changing dict
            end_time = data['end_time']
            remaining = int(end_time - now)

            if remaining > 0:
                status[str(pin)] = remaining
            else:
                set_relay_value(pin, 0)
                log_message(f"Zone {pin + 1} timer expired. Relay turned OFF.")
                if MQTT == 1:
                    publish_relay_status(client, pin, 0)
                expired.append(pin)

        # Remove expired timers
        for pin in expired:
            del timers[pin]

        #log_message(f"Timer status response: {status}")
        return ujson.dumps(status), {'Content-Type': 'application/json'}

    except Exception as e:
        log_message(f"Error in /timer-status: {e}")
        return '{}', 500


@app.route('/api/status', methods=['GET', 'OPTIONS'])
def api_status(request):
    if request.method == 'OPTIONS':
        return '', 200, {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, OPTIONS'
        }
    try:
        states = [relay.value() for relay in relays]
        
        now = time.time()
        status_dict = {}
        for pin, data in list(timers.items()):
            end_time = data['end_time']
            remaining = int(end_time - now)
            if remaining > 0:
                status_dict[str(pin)] = remaining
            
        global run_queue, current_queue_index, queue_active, current_zone_start_time
        remaining = 0
        if queue_active and 0 <= current_queue_index < len(run_queue):
            elapsed = time.time() - current_zone_start_time
            item = run_queue[current_queue_index]
            remaining = max(0, int(item['duration'] - elapsed))
            
        formatted_queue = []
        for item in run_queue:
            formatted_queue.append({
                "pin": item["pin"],
                "duration_mins": item["duration"] // 60
            })
            
        return ujson.dumps({
            "relay_states": states,
            "timers": status_dict,
            "queue": {
                "active": queue_active,
                "current_index": current_queue_index,
                "remaining_seconds": remaining,
                "steps": formatted_queue
            }
        }), 200, {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*'
        }
    except Exception as e:
        log_message(f"Error in /api/status route: {e}")
        return '{}', 500, {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*'
        }


@app.route('/cancel-timer/<pin>', methods=['GET', 'OPTIONS'])
def cancel_timer(request, pin):
    if request.method == 'OPTIONS':
        return '', 200, {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, OPTIONS'
        }
    try:
        pin = int(pin)
        stop_all_watering()
        return 'Timer canceled', 200, {'Access-Control-Allow-Origin': '*'}
    except Exception as e:
        log_message(f"Error in /cancel-timer route: {e}")
        return 'Failed to cancel timer', 500, {'Access-Control-Allow-Origin': '*'}


@app.route('/relay/<int:pin>/<state>', methods=['GET', 'OPTIONS'])
async def relay_toggle(request, pin, state):
    if request.method == 'OPTIONS':
        return '', 200, {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type'
        }
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
            return 'OK', 200, {'Access-Control-Allow-Origin': '*'}
        return 'Invalid pin', 400, {'Access-Control-Allow-Origin': '*'}
    except Exception as e:
        log_message(f"Error in /relay route: {e}")
        return 'Error', 500, {'Access-Control-Allow-Origin': '*'}

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
        return ujson.dumps({"days_remaining": days}), {'Content-Type': 'application/json'}
    except:
        return ujson.dumps({"days_remaining": 0}), {'Content-Type': 'application/json'}



@app.route('/get-schedules')
def get_schedules(request):
    return ujson.dumps(schedules), {'Content-Type': 'application/json'}


def load_schedules():
    try:
        with open('schedules.json', 'r') as f:
            data = ujson.load(f)
        if not isinstance(data, list):
            data = [{"enabled": True, "schedules": []} for _ in RELAY_PINS]
        else:
            if len(data) < len(relays):
                data.extend([{"enabled": True, "schedules": []} for _ in range(len(relays) - len(data))])
            elif len(data) > len(relays):
                data = data[:len(relays)]
            for i in range(len(data)):
                if isinstance(data[i], dict) and "schedules" in data[i]:
                    if "enabled" not in data[i]:
                        data[i]["enabled"] = True
                    if not isinstance(data[i]["schedules"], list):
                        data[i]["schedules"] = []
                elif isinstance(data[i], dict):
                    old_enabled = data[i].get("enabled", True)
                    if "onTime" in data[i] or "offTime" in data[i]:
                        import random
                        sched_id = f"sch_{int(time.time())}_{random.randint(1000, 9999)}"
                        data[i] = {
                            "enabled": old_enabled,
                            "schedules": [
                                {
                                    "id": sched_id,
                                    "days": data[i].get("days", []),
                                    "onTime": data[i].get("onTime", ""),
                                    "offTime": data[i].get("offTime", ""),
                                    "enabled": True
                                }
                            ]
                        }
                    else:
                        data[i] = {
                            "enabled": True,
                            "schedules": []
                        }
                elif isinstance(data[i], list):
                    data[i] = {
                        "enabled": True,
                        "schedules": data[i]
                    }
                else:
                    data[i] = {
                        "enabled": True,
                        "schedules": []
                    }
        return data
    except (OSError, ValueError):
        return [{"enabled": True, "schedules": []} for _ in RELAY_PINS]

def save_schedules(schedules_data):
    try:
        with open('schedules.json', 'w') as f:
            ujson.dump(schedules_data, f)
    except Exception as e:
        log_message(f"Failed to save schedules: {e}")

# Load or create a schedule store
schedules = load_schedules()

async def connect_to_wifi(fallback_to_ap=True):
    max_wifi_attempts = 10
    retry_delay = 2
    attempt_count = 0

    if not SSID or SSID == "":
        if fallback_to_ap:
            log_message("SSID is empty. Entering AP mode directly.")
            await enter_AP_mode()
        else:
            log_message("SSID is empty. Cannot connect.")
        return

    while attempt_count < max_wifi_attempts:
        try:
            ap = network.WLAN(network.AP_IF)
            ap.active(False)

            wifi = network.WLAN(network.STA_IF)
            wifi.active(True)
            wifi.config(pm=0)
            wifi.config(dhcp_hostname="sprinklers")

            wifi.connect(SSID, PASSWORD)

            for _ in range(5):
                if wifi.isconnected():
                    break
                await asyncio.sleep(1)

            if wifi.isconnected():
                log_message('Connected to Wi-Fi as sprinklers.local')
                try:
                    print('Wifi connected as sprinklers.local, net={}, gw={}, dns={}'.format(*wifi.ifconfig()))
                except OSError:
                    pass
                return  # Successfully connected
            else:
                log_message(f"Wi-Fi connection attempt {attempt_count + 1} failed. Retrying...")
                attempt_count += 1
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)
                gc.collect()

        except Exception as e:
            log_message(f"Exception during Wi-Fi connection: {e}")
            attempt_count += 1
            await asyncio.sleep(retry_delay)

    if fallback_to_ap:
        log_message('Failed to connect to Wi-Fi after all attempts. Entering AP mode as a last resort.')
        await enter_AP_mode()
    else:
        log_message('Failed to connect to Wi-Fi. Will retry in background...')

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



async def sync_time():
    while True:
        try:
            ntptime.settime()  # set RTC to UTC by default
            log_message("Time synchronized with NTP server.")

            base_offset = int(config.get("timezone", -7))
            
            now_utc = time.localtime()
            year, month, day, hour, minute, second, weekday, yearday = now_utc
            
            # DST applies only if not UTC
            dst_active = is_dst(year, month, day, weekday) if base_offset != 0 else False
            offset = base_offset + (1 if dst_active else 0)

            log_message(f"DST active: {dst_active}. Offset: UTC{offset}")

            # Adjust RTC to reflect local time
            adjusted_time = time.localtime(time.mktime(now_utc) + offset * 3600)
            machine.RTC().datetime((adjusted_time[0], adjusted_time[1], adjusted_time[2],
                                    adjusted_time[6]+1, adjusted_time[3], adjusted_time[4], adjusted_time[5], 0))
            log_message(f"RTC adjusted to local time: {adjusted_time}")

        except Exception as e:
            log_message(f"Failed to sync time: {e}")

        await asyncio.sleep(86400)  # once per day
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
        with open("rain_delay.json", "w") as f:
            ujson.dump({"days_remaining": remaining}, f)
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
                    set_relay_value(pin, 0)
                    log_message(f"Zone {pin+1} timer expired. Relay turned OFF.")
                    if MQTT == 1:
                        publish_relay_status(client, pin, 0)
                    expired.append(pin)

            for pin in expired:
                del timers[pin]

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

def publish_discovery(client):
    try:
        base_topic = "homeassistant/switch/SC{}"
        for i in range(len(relays)):
            topic = base_topic.format(i) + "/config"
            payload = {
                "name": f"Zone {i + 1}",
                "command_topic": f"cmnd/zone/{i}/power",
                "state_topic": f"stat/zone/{i}/state",
                "payload_on": "ON",
                "payload_off": "OFF",
                "unique_id": f"intellidwellSC{i}",
                "device": {
                    "identifiers": [f"intellidwellSC"],
                    "name": f"Sprinkler Controller",
                    "manufacturer": "Intellidwell",
                    "model": "Sprinkler Controller V1.0",
                    "sw_version": "1.0"
                },
                "availability_topic": f"{TOPIC_BASE}status",
                "payload_available": "Online",
                "payload_not_available": "Offline",
                "platform": "mqtt"
            }
            client.publish(topic, ujson.dumps(payload), retain=True)
        log_message("Published MQTT discovery messages")
    except Exception as e:
        log_message(f"Failed to publish discovery: {e}")

def publish_relay_status(client, relay, status):
    try:
        topic = f"stat/zone/{relay}/state"
        payload = "ON" if status else "OFF"
        client.publish(topic, payload)
        log_message(f"Published relay status for relay {relay}: {payload}")
    except Exception as e:
        log_message(f"Failed to publish relay status: {e}")

def update_schedule_status(pin, status):
    try:
        if 0 <= pin < len(schedules):
            schedules[pin]['enabled'] = status
            save_schedules(schedules)
            log_message(f"Schedule for Relay {pin+1} {'enabled' if status else 'disabled'}!")
            return True
        return False
    except Exception as e:
        log_message(f"Failed to update schedule status: {e}")
        return False

def disconnect_from_wifi():
    wlan = network.WLAN(network.STA_IF)
    if wlan.isconnected():
        log_message("Disconnecting from WiFi...")
        wlan.disconnect()
        time.sleep(1)
    log_message("Disconnected from WiFi")

async def enter_AP_mode():
    try:
        disconnect_from_wifi()
        wifi = network.WLAN(network.STA_IF)
        wifi.active(False)
        ap_ssid = "intelidwellSC"
        ap_password = "Sprinkler12345"
        
        ap = network.WLAN(network.AP_IF)
        ap.active(True)
        ap.config(essid=ap_ssid, password=ap_password, authmode=3)

        log_message(f"Configuration mode activated. Connect to AP: {ap_ssid} with password:{ap_password}")
        log_message("Visit http://192.168.4.1 in your web browser to configure.")

        # Start the web server to handle the configuration page
        await app.start_server(host='0.0.0.0', port=80)
        while True:
            await asyncio.sleep(300)
            reset()
    except Exception as e:
        log_message(f"Failed to enter AP mode: {e}")

def publish_schedule_discovery(client):
    try:
        base_topic = "homeassistant/switch/SS{}"
        for i in range(len(relays)):
            topic = base_topic.format(i) + "/config"
            payload = {
                "name": f"Zone {i+1} Scheduler",
                "command_topic": f"cmnd/zone/{i}/schedule",
                "state_topic": f"stat/zone/{i}/schedule",
                "payload_on": "true",
                "payload_off": "false",
                "unique_id": f"intellidwellSS{i}",
                "device": {
                    "identifiers": [f"intellidwellSS"],
                    "name": f"Sprinkler Scheduler",
                    "manufacturer": "Intellidwell",
                    "model": "Sprinkler Controller V1.0",
                    "sw_version": "1.0"
                },
                "availability_topic": f"{TOPIC_BASE}status",
                "payload_available": "Online",
                "payload_not_available": "Offline",
                "platform": "mqtt"
            }
            client.subscribe(f"cmnd/zone/{i}/schedule")
            client.publish(topic, ujson.dumps(payload), retain=True)
        log_message("Published schedule MQTT discovery messages")
    except Exception as e:
        log_message(f"Failed to publish schedule discovery: {e}")
    
def publish_schedule_status(client, pin, enabled):
    try:
        topic = f"stat/zone/{pin}/schedule"
        payload = "true" if enabled else "false"
        client.publish(topic, payload)
        log_message(f"Published schedule status for relay {pin}: {payload}")
    except Exception as e:
        log_message(f"Failed to publish schedule status: {e}")




async def run_server():
    log_message("Starting Microdot server...")
    try:
        await app.start_server(host='0.0.0.0', port=80)
    except Exception as e:
        log_message(f"Failed to start or run Microdot server: {e}")

async def mqtt_ping_loop():
    while True:
        try:
            if MQTT == 1 and is_mqtt_connected:
                client.ping()
        except Exception as e:
            log_message(f"MQTT ping failed: {e}")
        await asyncio.sleep(20)

async def wifi_monitor_loop():
    while True:
        try:
            wifi = network.WLAN(network.STA_IF)
            if not wifi.isconnected():
                log_message("Wi-Fi connection lost! Attempting to reconnect...")
                try:
                    wifi.disconnect()
                except:
                    pass
                await asyncio.sleep(2)
                await connect_to_wifi(fallback_to_ap=False)
        except Exception as e:
            log_message(f"Error in wifi_monitor_loop: {e}")
        await asyncio.sleep(30)

async def main():
    if MQTT == 0:
        await main_without_mqtt()
        return
    try:
        disconnect_from_wifi()
        await connect_to_wifi()
        await connect_mqtt()
        await asyncio.gather(
            run_server(),
            sync_time(),
            check_schedules(),
            check_messages(),
            run_queue_processor(),
            mqtt_ping_loop(),
            wifi_monitor_loop()
        )
    except Exception as e:
        log_message(f"Error in main loop: {e}")
        await main_without_mqtt()

async def main_without_mqtt():
    global MQTT
    MQTT = 0
    try:
        disconnect_from_wifi()
        await connect_to_wifi()
        await asyncio.gather(
            run_server(),
            sync_time(),
            check_schedules(),
            run_queue_processor(),
            wifi_monitor_loop()
        )
    except Exception as e:
        log_message(f"Error found again. Restarting without MQTT or WIFI: {e}")
        await main_without_mqtt_or_wifi()

async def main_without_mqtt_or_wifi():
    global MQTT
    MQTT = 0
    try:
        await asyncio.gather(
            sync_time(),
            check_schedules(),
            run_ap_mode(),
            run_queue_processor()
        )
    except Exception as e:
        log_message(f"Serious error: {e}")

async def run_ap_mode():
    log_message("Entering AP mode...")
    await enter_AP_mode()

try:
    asyncio.run(main())
except Exception as e:
    log_message(f"Error in startup: {e}")
    try:
        asyncio.run(main_without_mqtt())
    except Exception as e:
        log_message(f"Error in main_without_mqtt: {e}")
        try:
            asyncio.run(main_without_mqtt_or_wifi())
        except Exception as e:
            log_message(f"Serious error in final recovery: {e}")