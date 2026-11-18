import gc
gc.collect()

import network
import time
import ujson
import uos

def check_force_ap():
    try:
        uos.stat('force_ap.json')
        try:
            uos.remove('force_ap.json')
        except Exception:
            pass
        return True
    except Exception:
        return False

ssid = ""
password = ""
try:
    with open('settings.json', 'r') as f:
        cfg = ujson.load(f)
        ssid = cfg.get('ssid', '')
        password = cfg.get('wifi_password', '')
except Exception:
    pass

forced = check_force_ap()
wifi_connected = False

if not forced and ssid:
    try:
        try:
            network.hostname("sprinklers")
        except Exception:
            pass
        sta = network.WLAN(network.STA_IF)
        try:
            sta.config(dhcp_hostname="sprinklers")
        except Exception:
            pass
        sta.active(True)
        try:
            sta.config(dhcp_hostname="sprinklers")
        except Exception:
            pass
        try:
            sta.config(pm=0)
        except Exception:
            pass
        try:
            # Lock to 20MHz 802.11b (1-11 Mbps DSSS) & 15dBm TX: eliminates 11n/11g rate-collapse stalls on weak RSSI
            sta.config(protocol=network.MODE_11B)
        except Exception:
            pass
        for _cfg_k, _cfg_v in (('bandwidth', 1), ('txpower', 15)):
            try:
                sta.config(**{_cfg_k: _cfg_v})
            except Exception:
                pass
        _bssid = None
        try:
            _t = ssid.encode() if isinstance(ssid, str) else ssid
            _r = -999
            for _ap in sta.scan():
                if _ap[0] == _t and _ap[3] > _r:
                    _r = _ap[3]
                    _bssid = _ap[1]
        except Exception:
            pass
        if _bssid:
            sta.connect(ssid, password, bssid=_bssid)
        else:
            sta.connect(ssid, password)
        for _ in range(80):
            if sta.isconnected() and sta.ifconfig()[0] != '0.0.0.0':
                wifi_connected = True
                try:
                    sta.config(pm=0)
                except Exception:
                    pass
                break
            time.sleep(0.5)
    except Exception:
        pass

if wifi_connected:
    try:
        ap = network.WLAN(network.AP_IF)
        if ap.active():
            ap.active(False)
    except Exception:
        pass

if not wifi_connected:
    try:
        ap = network.WLAN(network.AP_IF)
        ap.active(True)
        ap.config(essid="intelidwellSC", password="Sprinkler12345", authmode=3)
        try:
            ap.ifconfig(('192.168.4.1', '255.255.255.0', '192.168.4.1', '192.168.4.1'))
        except Exception:
            pass
    except Exception:
        pass

gc.collect()
try:
    import controller

    if not wifi_connected:
        controller.EARLY_AP_MODE = True

    controller.start()
except Exception as e:
    try:
        print("Fatal startup error:", e)
    except:
        pass
    try:
        ap = network.WLAN(network.AP_IF)
        ap.active(True)
        ap.config(essid="intelidwellSC", password="Sprinkler12345", authmode=3)
        ap.ifconfig(('192.168.4.1', '255.255.255.0', '192.168.4.1', '192.168.4.1'))
    except Exception:
        pass
    time.sleep(10)
    import machine
    machine.reset()
