import ujson
import gc
import time
import uasyncio as asyncio
import network
import ubinascii

CREDENTIALS_FILE = "cloud_credentials.json"
DEFAULT_CLOUD_SERVER = "sprinklers.intellidwell.net"
DEFAULT_CLOUD_PORT = 80


def get_hardware_uid():
    try:
        w = network.WLAN(network.STA_IF)
        mac = w.config('mac')
        if mac:
            return ubinascii.hexlify(mac).decode('utf-8').lower()
    except Exception:
        pass
    return "8c94df4cf854"


def load_cloud_credentials():
    try:
        with open(CREDENTIALS_FILE, 'r') as f:
            data = ujson.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def save_cloud_credentials(data):
    try:
        with open(CREDENTIALS_FILE, 'w') as f:
            ujson.dump(data, f)
    except Exception as e:
        print(f"Error saving cloud credentials: {e}")


def make_ws_frame(opcode, data):
    if isinstance(data, str):
        data = data.encode('utf-8')
    elif data is None:
        data = b""
    length = len(data)
    b1 = 0x80 | (opcode & 0x0F)
    if length <= 125:
        header = bytes([b1, 0x80 | length, 0, 0, 0, 0])
    else:
        header = bytes([b1, 0x80 | 126, (length >> 8) & 0xFF, length & 0xFF, 0, 0, 0, 0])
    return header + data


def make_ws_text_frame(payload_str):
    return make_ws_frame(0x1, payload_str)


async def _wait_for(coro, timeout):
    return await asyncio.wait_for(coro, timeout)


async def read_ws_frame(reader):
    try:
        header = await reader.read(2)
    except Exception:
        return None, None
    if not header or len(header) < 2:
        return None, None
    b1, b2 = header[0], header[1]
    opcode = b1 & 0x0F
    is_masked = (b2 & 0x80) != 0
    payload_len = b2 & 0x7F
    if payload_len == 126:
        ext = await reader.read(2)
        if not ext or len(ext) < 2:
            return None, None
        payload_len = (ext[0] << 8) | ext[1]
    elif payload_len == 127:
        ext = await reader.read(8)
        if not ext or len(ext) < 8:
            return None, None
        payload_len = 0
        for b in ext:
            payload_len = (payload_len << 8) | b

    mask = None
    if is_masked:
        mask = await reader.read(4)
        if not mask or len(mask) < 4:
            return None, None

    buf = bytearray(payload_len)
    mv = memoryview(buf)
    pos = 0
    while pos < payload_len:
        chunk = await reader.read(min(payload_len - pos, 512))
        if not chunk:
            return None, None
        n = len(chunk)
        mv[pos:pos + n] = chunk
        pos += n

    if is_masked and mask:
        for i in range(payload_len):
            buf[i] ^= mask[i & 3]

    return opcode, bytes(buf)


async def _close_stream(stream):
    if not stream:
        return
    try:
        await asyncio.sleep_ms(50)
    except Exception:
        pass
    s = getattr(stream, 's', None)
    if s:
        try:
            import uasyncio.core as _core
            ioq = getattr(_core, '_io_queue', None)
            if ioq:
                if id(s) in ioq.map:
                    sm = ioq.map[id(s)]
                    if sm[0] is not None:
                        sm[0].data = None
                    if sm[1] is not None:
                        sm[1].data = None
                    del ioq.map[id(s)]
                try:
                    ioq.poller.unregister(s)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            s.close()
        except Exception:
            pass


async def safe_open_connection(host, port, timeout_s=3.0):
    import usocket as socket
    import select
    sock = None
    poll = None
    try:
        if host and host[0].isdigit():
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            addr = (host, int(port))
        else:
            ai = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)[0]
            sock = socket.socket(ai[0], ai[1], ai[2])
            addr = ai[-1]
        sock.setblocking(False)
        try:
            sock.connect(addr)
        except OSError as e:
            from errno import EINPROGRESS
            if e.args and e.args[0] != EINPROGRESS:
                raise e

        poll = select.poll()
        poll.register(sock, select.POLLOUT | select.POLLERR | select.POLLHUP)
        t_start = time.time()
        connected = False
        while time.time() - t_start < timeout_s:
            events = poll.poll(0)
            if events:
                for s, ev in events:
                    if ev & select.POLLOUT:
                        connected = True
                        break
                    elif ev & (select.POLLERR | select.POLLHUP):
                        raise OSError("POLLERR")
            if connected:
                break
            await asyncio.sleep_ms(20)

        try:
            poll.unregister(sock)
        except Exception:
            pass

        if not connected:
            raise OSError("Timeout connecting to server")

        stream = asyncio.StreamWriter(sock)
        return stream, stream
    except Exception as e:
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
        raise e


class CloudClient:
    def __init__(self, config, controller, log_func, ver_str="2.1.2"):
        self.config = config
        self.ctrl = controller
        self.log = log_func
        self.version = ver_str
        self.credentials = load_cloud_credentials()
        self.status = "disconnected"
        self.last_error = None
        self.writer = None
        self._running = False
        self._connected = False
        self._sync_pending = False
        self._sync_full = False

    def is_enabled(self):
        return int(self.config.get("cloud_enabled", 0)) == 1

    def is_connected(self):
        return self._connected and self.writer is not None

    def get_status_info(self):
        return {
            "enabled": self.is_enabled(),
            "connected": self.is_connected(),
            "status": self.status,
            "username": self.credentials.get("username", ""),
            "device_id": self.credentials.get("device_id", ""),
            "last_error": self.last_error,
        }

    async def send_json(self, data_dict):
        while getattr(self, '_sending', False) and self._connected:
            await asyncio.sleep_ms(20)
        if not self.is_connected() or not self.writer:
            return False
        self._sending = True
        try:
            gc.collect()
            raw = ujson.dumps(data_dict).encode('utf-8')
            length = len(raw)
            if length <= 125:
                hdr = bytes([0x81, 0x80 | length, 0, 0, 0, 0])
            else:
                hdr = bytes([0x81, 0xFE, (length >> 8) & 0xFF, length & 0xFF, 0, 0, 0, 0])
            if length <= 384:
                self.writer.write(hdr + raw)
                raw = None
                await _wait_for(self.writer.drain(), 5.0)
            else:
                self.writer.write(hdr)
                await _wait_for(self.writer.drain(), 5.0)
                mv = memoryview(raw)
                for i in range(0, length, 512):
                    self.writer.write(bytes(mv[i:i + 512]))
                    await _wait_for(self.writer.drain(), 5.0)
                    if i + 512 < length:
                        await asyncio.sleep_ms(15)
                mv = None
                raw = None
            gc.collect()
            return True
        except Exception as e:
            self.log(f"Cloud send error: {type(e).__name__}: {e}")
            self._connected = False
            w = self.writer
            self.writer = None
            await _close_stream(w)
            return False
        finally:
            self._sending = False

    def request_sync(self, full=False):
        if full:
            self._sync_full = True
        if not self.is_connected() or self._sync_pending:
            return
        self._sync_pending = True
        try:
            asyncio.create_task(self._deferred_sync())
        except Exception:
            self._sync_pending = False

    async def _deferred_sync(self):
        try:
            await asyncio.sleep_ms(150)
            full = self._sync_full
            self._sync_full = False
            await self.sync_state(full=full)
        finally:
            self._sync_pending = False

    async def sync_state(self, full=True):
        if not self.is_connected():
            return
        try:
            gc.collect()
            if hasattr(self.ctrl, "get_sync_data"):
                parts = (False, "settings", "schedules_0", "schedules_1") if full else (False,)
                for p in parts:
                    if not self.is_connected():
                        break
                    if p is not False:
                        await asyncio.sleep_ms(150)
                    payload = self.ctrl.get_sync_data(full=p)
                    await self.send_json(payload)
                    del payload
                    gc.collect()
            else:
                await self.send_json({
                    "type": "sync",
                    "zones": self.ctrl.get_zone_names() if hasattr(self.ctrl, "get_zone_names") else [],
                    "relays": self.ctrl.get_relay_values() if hasattr(self.ctrl, "get_relay_values") else [],
                    "rain_delay": self.ctrl.get_rain_delay_days() if hasattr(self.ctrl, "get_rain_delay_days") else 0,
                    "firmware_version": self.version
                })
        except Exception as e:
            self.log(f"Cloud sync error: {e}")

    async def login(self, username, password):
        server = self.config.get("cloud_server", DEFAULT_CLOUD_SERVER) or DEFAULT_CLOUD_SERVER
        port = int(self.config.get("cloud_port", DEFAULT_CLOUD_PORT) or DEFAULT_CLOUD_PORT)
        hw_uid = get_hardware_uid()

        self.status = "logging_in"
        self.log(f"Attempting cloud login to {server}:{port} as {username}...")

        servers_to_try = []
        try:
            sta = network.WLAN(network.STA_IF)
            if sta.isconnected() and sta.ifconfig()[0].startswith("192.168.1."):
                servers_to_try.append("192.168.1.74")
        except Exception:
            pass
        if server not in servers_to_try:
            servers_to_try.append(server)

        reader, writer = None, None
        for srv in servers_to_try:
            try:
                reader, writer = await safe_open_connection(srv, port, timeout_s=3.0)
                break
            except Exception as e:
                self.log(f"Login connect to {srv} failed: {e}")
                reader, writer = None, None

        if not reader or not writer:
            self.status = "error"
            self.last_error = "Connection failed"
            return False, "Failed to connect to cloud server"

        try:
            body = ujson.dumps({
                "username": username.strip(),
                "password": password.strip(),
                "hardware_uid": hw_uid,
                "device_name": f"IntelliDwell ({hw_uid[-6:]})",
                "device_model": "Classic-ESP32",
                "firmware_version": self.version
            }).encode('utf-8')

            req = (
                f"POST /v1/device/login HTTP/1.1\r\n"
                f"Host: {server}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"Connection: close\r\n\r\n"
            ).encode('utf-8') + body

            writer.write(req)
            await writer.drain()

            raw_resp = b""
            while True:
                chunk = await reader.read(512)
                if not chunk:
                    break
                raw_resp += chunk
                if len(raw_resp) > 4096:
                    break

            await _close_stream(writer)

            if b"\r\n\r\n" in raw_resp:
                head, body_bytes = raw_resp.split(b"\r\n\r\n", 1)
                first_line = head.split(b"\r\n")[0].decode('utf-8', 'ignore')
                if "200" in first_line:
                    res_json = ujson.loads(body_bytes.decode('utf-8', 'ignore'))
                    self.credentials = {
                        "device_id": res_json.get("device_id"),
                        "api_key": res_json.get("api_key"),
                        "username": res_json.get("username", username),
                        "user_id": res_json.get("user_id")
                    }
                    save_cloud_credentials(self.credentials)
                    self.status = "paired"
                    self.last_error = None
                    self.log(f"Cloud login successful! Device ID: {self.credentials.get('device_id')}")
                    return True, "Connected successfully"
                else:
                    err_msg = first_line
                    try:
                        err_json = ujson.loads(body_bytes.decode('utf-8', 'ignore'))
                        err_msg = err_json.get("detail", first_line)
                    except Exception:
                        pass
                    self.status = "error"
                    self.last_error = err_msg
                    self.log(f"Cloud login rejected: {err_msg}")
                    return False, err_msg

            self.status = "error"
            self.last_error = "Invalid server response"
            return False, "Invalid server response"

        except Exception as e:
            await _close_stream(writer)
            self.status = "error"
            self.last_error = str(e)
            self.log(f"Cloud login exception: {e}")
            return False, str(e)

    async def _handle_command(self, cmd_dict):
        cmd_id = cmd_dict.get("command_id")
        cmd_type = cmd_dict.get("command_type")
        payload = cmd_dict.get("payload") or {}
        self.log(f"Cloud command: {cmd_type}")

        try:
            if cmd_type in ("start_zone", "switch_zone"):
                zone_id = int(payload.get("zone_id", payload.get("output_id", 0)))
                runtime = int(payload.get("runtime_seconds", 0))
                self.ctrl.start_zone(zone_id, runtime)

            elif cmd_type == "stop_zone":
                zone_id = int(payload.get("zone_id", payload.get("output_id", 0)))
                self.ctrl.stop_zone(zone_id)

            elif cmd_type in ("stop_all", "stop_watering", "stop_run_queue"):
                self.ctrl.stop_all_watering()

            elif cmd_type == "start_run_queue":
                self.ctrl.start_queue(payload.get("queue", []))

            elif cmd_type in ("set_rain_delay", "rain_delay"):
                days = int(payload.get("days", payload.get("rain_delay_days", 0)))
                self.ctrl.set_rain_delay_days(days)

            elif cmd_type == "update_schedule":
                act = payload.get("action", "add_or_edit")
                pin = int(payload.get("zone_id", 0))
                self.ctrl.schedule_op(act, pin, payload)

            elif cmd_type == "delete_schedule":
                pin = int(payload.get("zone_id", 0))
                self.ctrl.schedule_op("delete", pin, payload)

            elif cmd_type in ("update_zone", "update_device_config", "update_master_valve", "update_network_config", "update_mqtt"):
                self.ctrl.update_config(cmd_type, payload)

            elif cmd_type == "clear_logs":
                self.ctrl.clear_logs()

            elif cmd_type == "start_ota":
                ver = payload.get("version") or payload.get("target_version") or payload.get("channel") or "beta"
                self.ctrl.start_ota(ver)

            elif cmd_type in ("sync_shadow", "sync_zones", "sync"):
                self.request_sync(True)

            elif cmd_type in ("reboot", "restart"):
                self.ctrl.reset()

            # Send acknowledgment back to cloud
            if cmd_id:
                await self.send_json({
                    "type": "ack",
                    "command_id": cmd_id,
                    "status": "acknowledged"
                })

        except Exception as e:
            self.log(f"Error handling cloud command {cmd_type}: {e}")

    async def _connect_ws(self):
        server = self.config.get("cloud_server", DEFAULT_CLOUD_SERVER) or DEFAULT_CLOUD_SERVER
        port = int(self.config.get("cloud_port", DEFAULT_CLOUD_PORT) or DEFAULT_CLOUD_PORT)
        device_id = self.credentials.get("device_id")
        api_key = self.credentials.get("api_key", "")

        if not device_id:
            self.status = "unpaired"
            return None, None

        self.status = "connecting"
        self.log(f"Connecting cloud WebSocket to {server}:{port}...")

        servers_to_try = []
        try:
            sta = network.WLAN(network.STA_IF)
            if sta.isconnected() and sta.ifconfig()[0].startswith("192.168.1."):
                servers_to_try.append("192.168.1.74")
        except Exception:
            pass
        if "192.168.1.74" not in servers_to_try and server not in servers_to_try:
            servers_to_try.append(server)

        reader, writer = None, None
        for srv in servers_to_try:
            try:
                reader, writer = await safe_open_connection(srv, port, timeout_s=3.0)
                break
            except Exception as e:
                self.log(f"WS connect to {srv} failed: {e}")
                reader, writer = None, None

        if not reader or not writer:
            return None, None

        try:
            path = f"/v1/device/{device_id}/ws"
            req = (
                f"GET {path}?api_key={api_key} HTTP/1.1\r\n"
                f"Host: {server}\r\n"
                f"Upgrade: websocket\r\n"
                f"Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                f"Sec-WebSocket-Version: 13\r\n"
                f"X-Device-Api-Key: {api_key}\r\n\r\n"
            )
            writer.write(req.encode('utf-8'))
            await _wait_for(writer.drain(), 3.0)

            # Read response headers until \r\n\r\n
            res = b""
            while b"\r\n\r\n" not in res:
                line = await _wait_for(reader.readline(), 5.0)
                if not line:
                    await _close_stream(writer)
                    return None, None
                res += line
                if len(res) > 2048:
                    await _close_stream(writer)
                    return None, None

            if b"101 " not in res and b"101\r\n" not in res:
                self.log("WebSocket upgrade rejected by server")
                await _close_stream(writer)
                return None, None

            return reader, writer
        except Exception as e:
            self.log(f"WS handshake failed: {e}")
            await _close_stream(writer)
            return None, None

    async def _ping_loop(self):
        while self._connected:
            await asyncio.sleep(20)
            if not self._connected:
                break
            if time.time() - getattr(self, '_last_rx_ts', time.time()) > 55:
                self.log("Cloud WebSocket keepalive timeout; closing stale socket...")
                self._connected = False
                w = self.writer
                self.writer = None
                await _close_stream(w)
                break
            await self.send_json({"type": "ping"})

    async def loop(self):
        self._running = True
        backoff = 5

        while self._running:
            try:
                gc.collect()

                if not self.is_enabled():
                    self.status = "disabled"
                    self._connected = False
                    await asyncio.sleep(10)
                    continue

                sta = network.WLAN(network.STA_IF)
                if not sta.active() or not sta.isconnected() or sta.ifconfig()[0] == '0.0.0.0':
                    self.status = "offline"
                    self._connected = False
                    await asyncio.sleep(5)
                    continue

                if not self.credentials.get("device_id"):
                    # Check if credentials are in config
                    u = self.config.get("cloud_username", "").strip()
                    p = self.config.get("cloud_password", "").strip()
                    if u and p:
                        ok, _ = await self.login(u, p)
                        if not ok:
                            await asyncio.sleep(30)
                            continue
                    else:
                        self.status = "unpaired"
                        await asyncio.sleep(15)
                        continue

                reader, writer = await self._connect_ws()
                if not reader or not writer:
                    self.status = "error"
                    self._connected = False
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    continue

                # Successfully connected!
                self.writer = writer
                self._connected = True
                self._last_rx_ts = time.time()
                self.status = "connected"
                self.last_error = None
                backoff = 5
                self.log("Cloud WebSocket connected!")

                # Sync initial state
                await self.sync_state()

                # Start background ping task
                ping_task = asyncio.create_task(self._ping_loop())

                try:
                    while self._connected:
                        opcode, payload = await read_ws_frame(reader)
                        if opcode is None:
                            self.log("Cloud WebSocket connection closed by remote")
                            break
                        self._last_rx_ts = time.time()

                        if opcode == 0x8:  # Close frame
                            self.log("Cloud WebSocket received close frame")
                            break
                        elif opcode == 0x9:  # Ping frame
                            # Send masked pong frame echoing ping payload (RFC 6455 Section 5.5.3)
                            try:
                                pong = make_ws_frame(0xA, payload if payload else b"")
                                writer.write(pong)
                                await _wait_for(writer.drain(), 3.0)
                            except Exception:
                                pass
                        elif opcode == 0x1:  # Text frame
                            try:
                                msg = ujson.loads(payload.decode('utf-8'))
                                msg_type = msg.get("type")
                                if msg_type == "command":
                                    await self._handle_command(msg)
                                elif msg_type == "pong":
                                    pass  # Ping acknowledged
                            except Exception as e:
                                self.log(f"Error parsing cloud frame: {e}")

                except Exception as e:
                    self.log(f"Cloud reader error: {e}")
                finally:
                    try:
                        ping_task.cancel()
                    except Exception:
                        pass

            except Exception as e:
                self.log(f"Cloud loop exception: {e}")
                self.last_error = str(e)

            finally:
                self._connected = False
                self.status = "disconnected"
                if self.writer:
                    await _close_stream(self.writer)
                    self.writer = None
                gc.collect()

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
