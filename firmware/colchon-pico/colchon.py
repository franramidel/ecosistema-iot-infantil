import json
import os
import time

import network
import ntptime
from machine import Pin

import secrets as cfg
from hx711 import HX711

try:
    import requests
except ImportError:
    import urequests as requests

hx = HX711(d_out=Pin(0), pd_sck=Pin(15))
hx.set_gain(128)
buzzer = Pin(13, Pin.OUT)
buzzer.value(0)

# --- VARIABLES DE CONFIGURACION ---
PESO_PAPA_GRAMOS = 250.0    # El peso que usamos para calibrar
UMBRAL_MOVIMIENTO = 15      # Gramos para ignorar el ruido de la base
UMBRAL_CAIDA = 0.30         # CA1: caida del 70% o mas del snapshot
LECTURAS_PROMEDIO = 10
FACTOR_CALIBRACION = 1.0


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
    if not wlan.isconnected():
        wlan.connect(cfg.WIFI_SSID, cfg.WIFI_PASSWORD)
        for _ in range(20):
            if wlan.isconnected():
                break
            time.sleep(1)
    print("Wi-Fi conectado:", wlan.isconnected(), wlan.ifconfig()[0] if wlan.isconnected() else "")
    try:
        ntptime.settime()
        print("Hora sincronizada")
    except Exception as e:
        print("No se pudo sincronizar la hora:", e)
    return wlan


def registrar_alerta_peso(gramos_actual):
    caida = int(round((1 - gramos_actual / PESO_PAPA_GRAMOS) * 100))
    cuerpo = {
        "event_id": uuid4(),
        "device_id": cfg.COLCHON_DEVICE_ID,
        "event_type": "ALERTA_PESO",
        "severity": "critical",
        "occurred_at": iso(time.time() - 2),
        "detail": "Caida de {}% del peso (snapshot {:.0f} g, ahora {:.0f} g)".format(caida, PESO_PAPA_GRAMOS, gramos_actual),
    }
    datos = json.dumps(cuerpo).encode("utf-8")
    cab = {"Content-Type": "application/json", "Authorization": "Bearer " + cfg.COLCHON_DEVICE_KEY}
    for intento in range(3):
        try:
            r = requests.post(cfg.SERVIDOR_URL.rstrip("/") + "/api/audit/events", data=datos, headers=cab)
            print("Auditoria:", r.status_code, r.text)
            r.close()
            return
        except Exception as e:
            print("Error en auditoria (intento {}):".format(intento + 1), e)
            time.sleep(1)


print("=========================================")
print("   SISTEMA DE COLCHON INTELIGENTE        ")
print("=========================================")
conectar()

print("\n--- PASO 1: CALIBRACION DE COLCHON VACIO ---")
print("Asegurate de que la cama este completamente vacia.")
input("Presiona ENTER cuando este lista para calibrar...")
print("Calibrando...")
time.sleep(1)
hx.tare()
print("Colchon calibrado a cero correctamente.\n")

print("--- PASO 2: REGISTRO DE PESO DEL BEBE ---")
print("Por favor, acuesta al bebe (pon la papa) sobre el colchon.")
input("Presiona ENTER cuando el bebe este quieto en la cama...")

print("Capturando masa agregada (Snapshot)...")
suma_cruda = 0
for _ in range(20):
    suma_cruda += hx.get_value()
    time.sleep(0.05)

valor_crudo_actual = abs(suma_cruda / 20)

if valor_crudo_actual < 500:
    print("\n[ERROR] El sensor no detecto peso. Revisa el montaje.")
else:
    FACTOR_CALIBRACION = valor_crudo_actual / PESO_PAPA_GRAMOS

    print("\n[INFO] Calibracion automatica exitosa.")
    print("Snapshot guardado. Peso base: {:.0f} gramos.".format(PESO_PAPA_GRAMOS))
    print("Iniciando monitoreo en tiempo real...\n")
    time.sleep(1)

    historial_lecturas = [PESO_PAPA_GRAMOS * FACTOR_CALIBRACION] * LECTURAS_PROMEDIO
    tiempo_ultimo_reporte = time.time()
    contador_movimiento = 0
    tiempo_inicio_monitoreo = time.time()
    bebe_en_cuna = True

    try:
        while True:
            lectura_cruda = hx.get_value()
            historial_lecturas.pop(0)
            historial_lecturas.append(lectura_cruda)

            valor_promedio_crudo = sum(historial_lecturas) / len(historial_lecturas)
            valor_actual_gramos = abs(valor_promedio_crudo) / FACTOR_CALIBRACION

            diferencia_gramos = abs(valor_actual_gramos - PESO_PAPA_GRAMOS)
            tiempo_transcurrido = time.time() - tiempo_inicio_monitoreo

            # --- LOGICA DE PRESENCIA ---
            if tiempo_transcurrido > 3:
                if valor_actual_gramos <= (PESO_PAPA_GRAMOS * UMBRAL_CAIDA):
                    if bebe_en_cuna:
                        print("\n[ALERTA] ¡EL BEBE YA NO ESTA EN LA CAMA! (Sustraccion)")
                        bebe_en_cuna = False
                        buzzer.value(1)                              # CA2: alarma primero
                        registrar_alerta_peso(valor_actual_gramos)   # CA3 + Telegram
                else:
                    if not bebe_en_cuna:
                        print("\n[INFO] Bebe detectado nuevamente. Reanudando monitoreo...")
                        buzzer.value(0)
                        historial_lecturas = [valor_actual_gramos * FACTOR_CALIBRACION] * LECTURAS_PROMEDIO
                        bebe_en_cuna = True
                        tiempo_inicio_monitoreo = time.time()

            # --- ALARMA EN PULSOS MIENTRAS NO ESTE EL BEBE ---
            if not bebe_en_cuna:
                buzzer.value((time.ticks_ms() // 300) % 2)

            # --- LOGICA DE MOVIMIENTO ---
            if bebe_en_cuna:
                if diferencia_gramos > UMBRAL_MOVIMIENTO:
                    estado_actual = "MOVIMIENTO"
                    contador_movimiento += 2
                else:
                    estado_actual = "DURMIENDO"
                    if contador_movimiento > 0:
                        contador_movimiento -= 1
            else:
                estado_actual = "AUSENTE"

            # --- REPORTE PERIODICO ---
            tiempo_actual = time.time()
            if tiempo_actual - tiempo_ultimo_reporte >= 5:
                nivel_movimiento = min(100, contador_movimiento * 5)

                if not bebe_en_cuna:
                    nivel_movimiento = 0

                print('-> [NUBE] Estado: {} | Mov: {}% | Peso: {:.0f} g'.format(
                    estado_actual, nivel_movimiento, valor_actual_gramos))

                tiempo_ultimo_reporte = tiempo_actual

                if contador_movimiento < 3:
                    contador_movimiento = 0
                else:
                    contador_movimiento = 5

            time.sleep(0.1)

    except KeyboardInterrupt:
        buzzer.value(0)
        print("\nMonitoreo detenido manualmente.")