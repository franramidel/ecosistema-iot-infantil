import json
import os
import time

import machine
import network
import ntptime
import secrets as cfg

try:
    import requests
except ImportError:
    import urequests as requests

PIN_PIR = 17
ESPERA_S = 10          # segundos mínimos entre disparos


def uuid4():
    b = bytearray(os.urandom(16))
    b[6] = (b[6] & 0x0F) | 0x40
    b[8] = (b[8] & 0x3F) | 0x80
    h = "".join("{:02x}".format(x) for x in b)
    return "{}-{}-{}-{}-{}".format(h[0:8], h[8:12], h[12:16], h[16:20], h[20:32])


def iso(segundos):
    t = time.gmtime(segundos)
    return "{:04d}-{:02d}-{:02d}T{:02d}:{:02d}:{:02d}.000000+00:00".format(t[0], t[1], t[2], t[3], t[4], t[5])


def conectar():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    try:
        wlan.config(pm=network.WLAN.PM_NONE)
    except Exception as e:
        print("No se pudo desactivar el ahorro de energia:", e)

def registrar_evento():
    cuerpo = {
        "event_id": uuid4(),
        "device_id": cfg.SEG_DEVICE_ID,
        "event_type": "MOVIMIENTO_CUARTO",
        "severity": "warning",
        "occurred_at": iso(time.time() - 2),   # 2 s atrás, por si hay diferencia de relojes
        "detail": "Movimiento detectado por el PIR",
    }
    try:
        r = requests.post(
            cfg.SERVIDOR_URL.rstrip("/") + "/api/audit/events",
            data=json.dumps(cuerpo).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + cfg.SEG_DEVICE_KEY})
        print("Auditoría:", r.status_code, r.text)
        r.close()
    except Exception as e:
        print("Error en auditoría:", e)


def disparar_camara():
    try:
        r = requests.get(cfg.CAMARA_URL)
        print("Cámara:", r.status_code, r.text)
        r.close()
    except Exception as e:
        print("Error con la cámara:", e)


wlan = conectar()
try:
    ntptime.settime()
    print("Hora sincronizada")
except Exception as e:
    print("No se pudo sincronizar la hora:", e)

pir = machine.Pin(PIN_PIR, machine.Pin.IN)
print("Calentando el PIR, espera 30 segundos...")
time.sleep(30)
print("Listo, vigilando")

ultimo = None
while True:
    if pir.value() == 1 and (ultimo is None or time.ticks_diff(time.ticks_ms(), ultimo) > ESPERA_S * 1000):
        print("MOVIMIENTO")
        registrar_evento()
        disparar_camara()
        ultimo = time.ticks_ms()
    time.sleep_ms(100)