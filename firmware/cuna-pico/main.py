"""Cuna inteligente: sensor DHT22 (temperatura y humedad) en una Raspberry Pi Pico W con MicroPython.

El dispositivo SOLO informa lecturas. La alerta la decide el servidor (RF-NOT-02) con los umbrales del bebé
(RF-CUN-01). Cada lectura es un evento con su propio UUID y su hora de captura, así que reenviarla no duplica nada.

Conexiones: DHT22 + -> 3V3(OUT) pin 36, - -> GND pin 38, OUT -> GP15 pin 20.
            LED opcional (solo pruebas) con resistencia de 330 ohm: GP14 pin 19 -> LED -> GND.

Qué se envía y cuándo (cada medida va como evento aparte: "Temperatura" en °C y "Humedad" en %):
  * cuando la temperatura cambia 0.5 °C o más desde el último envío,
  * al instante cuando cruza un umbral (el dispositivo consulta los umbrales al servidor cada minuto),
  * cada 30 s aunque no cambie nada, para que el panel sepa que el dispositivo sigue vivo.
Si no hay Wi-Fi o el servidor no responde, los eventos esperan en una cola de hasta 100 y se reenvían en orden con su
event_id y su captured_at ORIGINALES (RF-DAT-03). Lo que sigue pendiente también se guarda en el archivo cola.jsonl de la
placa, así que sobrevive a un reinicio. Ver LEEME.md, sección "Búfer sin conexión".
"""
import json
import os
import time

import dht
import machine

ENVIAR = False  # False = solo probar el circuito (Wokwi, sin Wi-Fi). True = conectar y enviar al servidor.

PIN_DHT = 15
PIN_LED = 14
INTERVALO_LECTURA = 2        # s. El DHT22 no admite menos
INTERVALO_LATIDO = 30        # s entre envíos aunque nada cambie
CAMBIO_MINIMO = 0.5          # °C que obligan a enviar
INTERVALO_UMBRALES = 60      # s entre consultas de umbrales
INTERVALO_HORA = 3600        # s entre sincronizaciones de la hora
MAX_PENDIENTES = 100         # eventos en espera. Cada uno ocupa unos 300 bytes de RAM; con 100 sobra memoria en la Pico W
COLA_ARCHIVO = "cola.jsonl"  # copia en flash de los eventos que siguen sin enviarse (una línea JSON por evento)
TEMP_MIN = 19.0              # valores por defecto del SRS, hasta conocer los del servidor
TEMP_MAX = 22.0


def uuid4():
    b = bytearray(os.urandom(16))
    b[6] = (b[6] & 0x0F) | 0x40
    b[8] = (b[8] & 0x3F) | 0x80
    h = "".join("{:02x}".format(x) for x in b)
    return "{}-{}-{}-{}-{}".format(h[0:8], h[8:12], h[12:16], h[16:20], h[20:32])


def ahora_iso():
    t = time.gmtime()
    return "{:04d}-{:02d}-{:02d}T{:02d}:{:02d}:{:02d}.000000+00:00".format(t[0], t[1], t[2], t[3], t[4], t[5])


def clasificar(temp, minimo, maximo):
    """Misma regla del servidor: solo > máximo o < mínimo (los límites exactos son normales)."""
    if temp > maximo:
        return 1  # calor
    if temp < minimo:
        return 2  # frío
    return 0


def eventos_de_lectura(temp, hum, id_dispositivo):
    at = ahora_iso()
    return [
        {"event_id": uuid4(), "device_id": id_dispositivo, "captured_at": at,
         "metric": "Temperatura", "value": round(temp, 1), "unit": "°C"},
        {"event_id": uuid4(), "device_id": id_dispositivo, "captured_at": at,
         "metric": "Humedad", "value": round(hum, 1), "unit": "%"},
    ]


def _existe(ruta):
    try:
        os.stat(ruta)
        return True
    except OSError:
        return False


def _borrar(ruta):
    if _existe(ruta):
        os.remove(ruta)


def cola_leer(ruta, maximo):
    """Lee los eventos guardados. Devuelve (eventos, lineas). Ignora líneas cortadas por un corte de energía,
    líneas que no son un evento completo y repetidos por event_id. Si falta el archivo pero quedó el temporal de una
    reescritura completa, recupera el temporal."""
    if not _existe(ruta) and _existe(ruta + ".tmp"):
        os.rename(ruta + ".tmp", ruta)
    eventos, vistos, lineas = [], set(), 0
    try:
        with open(ruta) as f:
            for linea in f:
                lineas += 1
                try:
                    ev = json.loads(linea)
                    ev_id = ev["event_id"]
                    for clave in ("device_id", "captured_at", "metric", "value", "unit"):
                        ev[clave]
                except (ValueError, KeyError, TypeError):
                    continue
                if ev_id in vistos:
                    continue
                vistos.add(ev_id)
                eventos.append(ev)
    except OSError:
        pass
    return eventos[-maximo:], lineas


class Cola:
    """Cola de eventos pendientes con copia en un archivo, para no perderlos si la placa se reinicia sin Wi-Fi.

    El evento NUNCA se modifica: conserva el event_id y el captured_at con que se creó, también tras un reinicio.
    Si algún reenvío se repite (por ejemplo, se reinicia justo después de enviar), el servidor lo reconoce por el
    event_id y no duplica nada. Para no desgastar la flash solo se escribe cuando algo queda sin enviar:
    se agregan líneas al final y el archivo se borra cuando la cola se vacía. Los eventos de un ciclo que se envían
    en ese mismo ciclo no tocan la flash; si hay un corte de energía antes de enviarlos, se pierden (unos 2 s de lecturas).
    """

    def __init__(self, ruta, maximo):
        self.ruta, self.maximo = ruta, maximo
        self.eventos, self.lineas = cola_leer(ruta, maximo)
        self.guardados = len(self.eventos)  # los primeros `guardados` eventos de la cola ya tienen su línea en el archivo
        self.reescribir = self.lineas != len(self.eventos)  # había líneas inservibles o repetidas: compactar

    def __len__(self):
        return len(self.eventos)

    def primero(self):
        return self.eventos[0]

    def quitar(self):
        self.eventos.pop(0)
        self.guardados = max(0, self.guardados - 1)

    def agregar(self, nuevos):
        self.eventos.extend(nuevos)
        sobran = len(self.eventos) - self.maximo
        if sobran > 0:
            del self.eventos[:sobran]  # si el servidor sigue caído, se pierden primero los más viejos
            self.guardados = max(0, self.guardados - sobran)
            self.reescribir = True

    def guardar(self):
        """Deja el archivo al día. Llamar al final de cada ciclo, después de intentar enviar."""
        if not self.eventos:
            if self.lineas or _existe(self.ruta):
                _borrar(self.ruta)
            _borrar(self.ruta + ".tmp")
            self.lineas = self.guardados = 0
            self.reescribir = False
            return
        nuevos = self.eventos[self.guardados:]
        if self.reescribir or self.lineas + len(nuevos) > 2 * self.maximo:
            # Se escribe completo en un temporal y luego se cambia por el archivo: un corte a mitad no pierde la cola.
            with open(self.ruta + ".tmp", "w") as f:
                for ev in self.eventos:
                    f.write(json.dumps(ev) + "\n")
            _borrar(self.ruta)
            os.rename(self.ruta + ".tmp", self.ruta)
            self.lineas, self.guardados, self.reescribir = len(self.eventos), len(self.eventos), False
        elif nuevos:
            with open(self.ruta, "a") as f:
                for ev in nuevos:
                    f.write(json.dumps(ev) + "\n")
            self.lineas += len(nuevos)
            self.guardados = len(self.eventos)


class Cliente:
    """Habla con el servidor (modo "servidor") o directamente con Supabase (modo "supabase")."""

    def __init__(self, cfg, requests):
        self.cfg, self.requests = cfg, requests
        self.modo = getattr(cfg, "MODO", "servidor")

    def _post(self, ruta, cuerpo):
        if self.modo == "supabase":
            url = self.cfg.SUPABASE_URL.rstrip("/") + "/rest/v1/rpc/" + ruta
            cabeceras = {"Content-Type": "application/json", "apikey": self.cfg.SUPABASE_KEY,
                         "Authorization": "Bearer " + self.cfg.SUPABASE_KEY}
        else:
            url = self.cfg.SERVIDOR_URL.rstrip("/") + ruta
            cabeceras = {"Content-Type": "application/json", "Authorization": "Bearer " + self.cfg.DEVICE_KEY}
        r = self.requests.post(url, data=json.dumps(cuerpo).encode("utf-8"), headers=cabeceras)
        try:
            return r.status_code, r.text
        finally:
            r.close()

    def enviar(self, ev):
        """Devuelve "ok", "reintentar" (red o servidor caído) o "descartar" (el servidor lo rechazó para siempre)."""
        try:
            if self.modo == "supabase":
                codigo, _ = self._post("cuna_ingest", {
                    "device": ev["device_id"], "secret": self.cfg.DEVICE_KEY, "event_id": ev["event_id"],
                    "at_time": ev["captured_at"], "metric_name": ev["metric"], "measurement": ev["value"],
                    "measurement_unit": ev["unit"]})
            else:
                codigo, texto = self._post("/api/events", ev)
        except Exception as e:  # sin Wi-Fi, DNS, tiempo de espera...
            print("Sin conexión:", e)
            return "reintentar"
        if 200 <= codigo < 300:
            return "ok"
        if codigo in (408, 425, 429) or codigo >= 500:
            return "reintentar"
        print("El servidor rechazó el evento ({}). Se descarta. Motivo: {}".format(codigo, texto if self.modo != "supabase" else ""))
        return "descartar"

    def umbrales(self, id_dispositivo):
        try:
            if self.modo == "supabase":
                codigo, texto = self._post("cuna_device_thresholds", {"device": id_dispositivo, "secret": self.cfg.DEVICE_KEY})
            else:
                codigo, texto = self._post("/api/device/thresholds", {"device_id": id_dispositivo})
            if codigo != 200:
                return None
            d = json.loads(texto)
            if isinstance(d, list):  # PostgREST puede envolver el resultado
                d = d[0]
            return float(d["temp_min"]), float(d["temp_max"])
        except Exception as e:
            print("No se pudieron leer los umbrales:", e)
            return None


def ejecutar(cfg=None, requests=None, ciclos=None):
    """Bucle principal. `ciclos` solo lo usan las pruebas en un computador; en la placa corre para siempre."""
    sensor = dht.DHT22(machine.Pin(PIN_DHT))
    led = machine.Pin(PIN_LED, machine.Pin.OUT)
    cliente = wlan = None
    id_dispositivo = "cuna-01"

    if ENVIAR:
        import network
        import ntptime
        if cfg is None:
            import secrets as cfg
        if requests is None:
            try:
                import requests
            except ImportError:
                import urequests as requests
        id_dispositivo = cfg.DEVICE_ID
        cliente = Cliente(cfg, requests)
        wlan = network.WLAN(network.STA_IF)

    def conectar():
        wlan.active(True)
        try:  # si quedó conectada a otra red (por ejemplo tras cambiar secrets.py), se suelta para usar la configurada
            if wlan.isconnected() and wlan.config("ssid") != cfg.WIFI_SSID:
                wlan.disconnect()
        except Exception:
            pass
        if not wlan.isconnected():
            wlan.connect(cfg.WIFI_SSID, cfg.WIFI_PASSWORD)
            for _ in range(20):
                if wlan.isconnected():
                    break
                time.sleep(1)
        if wlan.isconnected():
            print("Wi-Fi conectado. IP:", wlan.ifconfig()[0])
        else:
            print("No se pudo conectar al Wi-Fi")
        return wlan.isconnected()

    def sincronizar_hora():
        try:
            ntptime.settime()  # deja el reloj en UTC; el servidor rechaza capturas en el futuro
            print("Hora sincronizada:", ahora_iso())
            return True
        except Exception as e:
            print("No se pudo sincronizar la hora:", e)
            return False

    cola = Cola(COLA_ARCHIVO, MAX_PENDIENTES) if ENVIAR else None
    minimo, maximo = TEMP_MIN, TEMP_MAX
    hora_ok = False
    ultima_clase = None
    ultima_temp = None
    t_envio = t_umbrales = t_hora = None
    n = 0

    if ENVIAR:
        if len(cola):
            print("Eventos pendientes de antes del reinicio:", len(cola))
        conectar()
        hora_ok = sincronizar_hora()
        t_hora = time.ticks_ms()

    while ciclos is None or n < ciclos:
        n += 1
        try:
            sensor.measure()
            temp, hum = sensor.temperature(), sensor.humidity()
        except OSError:
            print("Error leyendo el sensor")
            time.sleep(INTERVALO_LECTURA)
            continue

        clase = clasificar(temp, minimo, maximo)
        led.value(1 if clase else 0)
        print("Temp: {:.1f} C, Hum: {:.1f} %".format(temp, hum), ("(calor)", "(frio)")[clase - 1] if clase else "")

        if ENVIAR:
            ms = time.ticks_ms()
            if not wlan.isconnected():
                conectar()
            if wlan.isconnected() and (not hora_ok or time.ticks_diff(ms, t_hora) >= INTERVALO_HORA * 1000):
                hora_ok = sincronizar_hora() or hora_ok
                t_hora = ms
            if wlan.isconnected() and (t_umbrales is None or time.ticks_diff(ms, t_umbrales) >= INTERVALO_UMBRALES * 1000):
                nuevos = cliente.umbrales(id_dispositivo)
                t_umbrales = ms
                if nuevos:
                    minimo, maximo = nuevos
                    clase = clasificar(temp, minimo, maximo)

            cruce = ultima_clase is not None and clase != ultima_clase
            cambio = ultima_temp is None or abs(temp - ultima_temp) >= CAMBIO_MINIMO
            latido = t_envio is None or time.ticks_diff(ms, t_envio) >= INTERVALO_LATIDO * 1000
            if hora_ok and (cruce or cambio or latido):
                cola.agregar(eventos_de_lectura(temp, hum, id_dispositivo))
                ultima_clase, ultima_temp, t_envio = clase, temp, ms

            while len(cola) and wlan.isconnected():
                resultado = cliente.enviar(cola.primero())
                if resultado == "reintentar":
                    break
                if resultado == "ok":
                    print("Enviado", cola.primero()["metric"])
                cola.quitar()
            try:
                cola.guardar()
            except OSError as e:  # flash llena o dañada: la cola sigue en memoria
                print("No se pudo guardar la cola:", e)

        time.sleep(INTERVALO_LECTURA)


if __name__ == "__main__":
    ejecutar()
