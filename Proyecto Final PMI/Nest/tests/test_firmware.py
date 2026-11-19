"""Pruebas del firmware de la Pico W (firmware/cuna-pico/main.py) contra el servidor real.

La placa no existe aquí: `machine`, `dht`, `network` y `ntptime` son sustitutos y el reloj es simulado. Lo que sí es
real es el código de main.py, el servidor, la base SQLite y la cola de alertas. Demuestran que el dispositivo cumple
el contrato de POST /api/events y que un cruce de umbral llega a la cola de Telegram en la misma lectura.
No prueban el sensor, el Wi-Fi ni MicroPython: eso se verifica con la placa.
"""
import contextlib
import importlib.util
import json
import sys
import types
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from apoyo import Base

RAIZ = Path(__file__).resolve().parent.parent.parent.parent
FIRMWARE = RAIZ / "firmware" / "cuna-pico" / "main.py"


class Respuesta:
    def __init__(self, codigo, texto):
        self.status_code, self.text = codigo, texto

    def close(self):
        pass


class Red:
    """Sustituto de `requests`: usa urllib de verdad contra el servidor de la prueba."""

    def __init__(self, fallos=0):
        self.fallos, self.llamadas = fallos, []  # fallos: cuántas llamadas seguidas fallan antes de que vuelva la red

    def post(self, url, data=None, headers=None):
        self.llamadas.append((url, json.loads(data), dict(headers or {})))
        if self.fallos > 0:
            self.fallos -= 1
            raise OSError("sin red")
        req = Request(url, data=data if isinstance(data, bytes) else data.encode(), headers=headers or {}, method="POST")
        try:
            with urlopen(req, timeout=5) as r:
                return Respuesta(r.status, r.read().decode())
        except HTTPError as e:
            return Respuesta(e.code, e.read().decode())
        except URLError as e:
            raise OSError(str(e))


class RedSimulada(Red):
    """Responde 200 a todo, sin servidor: para revisar solo cómo arma las llamadas el modo Supabase."""

    def post(self, url, data=None, headers=None):
        self.llamadas.append((url, json.loads(data), dict(headers or {})))
        return Respuesta(200, "{}")


class Sensor:
    def __init__(self, lecturas):
        self.lecturas, self.i = list(lecturas), -1

    def measure(self):
        self.i = min(self.i + 1, len(self.lecturas) - 1)

    def temperature(self):
        return self.lecturas[self.i][0]

    def humidity(self):
        return self.lecturas[self.i][1]


class Pin:
    OUT = 1

    def __init__(self, *args):
        self.v = 0

    def value(self, v=None):
        if v is not None:
            self.v = v
        return self.v


class Wlan:
    def __init__(self, *a):
        pass

    def active(self, *a):
        pass

    def isconnected(self):
        return True

    def connect(self, *a):
        pass

    def ifconfig(self):
        return ("127.0.0.1",)


class Reloj:
    def __init__(self):
        import time
        self.ms = 0
        self.gmtime = time.gmtime

    def sleep(self, s):
        self.ms += int(s * 1000)

    def ticks_ms(self):
        return self.ms

    def ticks_diff(self, a, b):
        return a - b


def cargar_firmware(sensor, reloj):
    """Importa main.py con los módulos de la placa sustituidos. Cada llamada da un módulo nuevo."""
    falsos = {"machine": types.SimpleNamespace(Pin=Pin),
              "dht": types.SimpleNamespace(DHT22=lambda pin: sensor),
              "network": types.SimpleNamespace(WLAN=Wlan, STA_IF=0),
              "ntptime": types.SimpleNamespace(settime=lambda: None)}
    with placa(falsos):
        spec = importlib.util.spec_from_file_location("pico_main_prueba", FIRMWARE)
        modulo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(modulo)
    modulo.time = reloj
    modulo.ENVIAR = True
    modulo.falsos = falsos
    return modulo


@contextlib.contextmanager
def placa(falsos):
    """Los módulos de la placa existen solo mientras corre el firmware. ejecutar() importa `network` y `ntptime` al arrancar."""
    previos = {k: sys.modules.get(k) for k in falsos}
    sys.modules.update(falsos)
    try:
        yield
    finally:
        for k, v in previos.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def ejecutar(fw, cfg, red, ciclos):
    with placa(fw.falsos):
        fw.ejecutar(cfg, red, ciclos)


class FirmwareBase(Base):
    def setUp(self):
        super().setUp()
        self.bebe = self.child("Bebé de prueba")
        self.seed_device(self.bebe, "CUNA-1")  # asignado desde hace tiempo: la hora del firmware se trunca al segundo
        self.red = Red()

    def cfg(self, **extra):
        datos = dict(WIFI_SSID="x", WIFI_PASSWORD="x", DEVICE_ID="CUNA-1", DEVICE_KEY=self.keys["CUNA-1"],
                     MODO="servidor", SERVIDOR_URL=self.url)
        datos.update(extra)
        return types.SimpleNamespace(**datos)

    def correr(self, lecturas, ciclos=None, cfg=None):
        fw = cargar_firmware(Sensor(lecturas), Reloj())
        ejecutar(fw, cfg or self.cfg(), self.red, ciclos if ciclos is not None else len(lecturas))
        return fw

    def eventos(self, metrica=None):
        with self.db.connection() as db:
            consulta = "select * from events" + (" where metric=?" if metrica else "") + " order by captured_at,rowid"
            return [dict(r) for r in db.execute(consulta, (metrica,) if metrica else ())]

    def cola(self):
        with self.db.connection() as db:
            return [dict(r) for r in db.execute("select * from notification_queue order by created_at,rowid")]


class TestContrato(FirmwareBase):
    def test_01_envia_temperatura_y_humedad_como_eventos_validos(self):
        self.correr([(21.0, 55.0)])
        temp, hum = self.eventos("Temperatura"), self.eventos("Humedad")
        self.assertEqual((len(temp), len(hum)), (1, 1))
        self.assertEqual((temp[0]["value"], temp[0]["unit"], temp[0]["device_id"], temp[0]["child_id"]),
                         (21.0, "°C", "CUNA-1", self.bebe))
        self.assertEqual((hum[0]["value"], hum[0]["unit"]), (55.0, "%"))
        envio = [c for c in self.red.llamadas if c[0].endswith("/api/events")][0]
        self.assertEqual(sorted(envio[1]), ["captured_at", "device_id", "event_id", "metric", "unit", "value"])
        self.assertEqual(envio[2]["Authorization"], "Bearer " + self.keys["CUNA-1"])

    def test_02_cada_evento_tiene_un_uuid_v4_distinto(self):
        import uuid
        self.correr([(21.0, 55.0)])
        ids = [c[1]["event_id"] for c in self.red.llamadas if c[0].endswith("/api/events")]
        self.assertEqual(len(set(ids)), 2)
        for i in ids:
            self.assertEqual(uuid.UUID(i).version, 4)

    def test_03_sin_cambios_solo_envia_el_primer_latido(self):
        self.correr([(21.0, 55.0)] * 10)  # 20 s simulados: menos que el latido de 30 s
        self.assertEqual(len(self.eventos("Temperatura")), 1)

    def test_04_el_latido_reenvia_aunque_no_cambie(self):
        self.correr([(21.0, 55.0)] * 20)  # 40 s simulados
        self.assertEqual(len(self.eventos("Temperatura")), 2)

    def test_05_un_cambio_de_medio_grado_se_envia_al_instante(self):
        self.correr([(21.0, 55.0), (21.2, 55.0), (21.5, 55.0)])
        self.assertEqual([e["value"] for e in self.eventos("Temperatura")], [21.0, 21.5])

    def test_06_dispositivo_con_clave_mala_descarta_y_no_reintenta_siempre(self):
        fw = self.correr([(21.0, 55.0)] * 3, cfg=self.cfg(DEVICE_KEY="clave-equivocada"))
        self.assertEqual(self.eventos(), [])
        envios = [c for c in self.red.llamadas if c[0].endswith("/api/events")]
        self.assertEqual(len(envios), 2)  # los dos eventos del primer latido, una vez cada uno

    def test_07_sin_red_los_eventos_esperan_y_se_reenvian_con_el_mismo_uuid(self):
        self.red.fallos = 3  # umbrales, primer envío y primer reintento fallan; el segundo reintento ya pasa
        self.correr([(21.0, 55.0)] * 4)
        intentos = [c[1]["event_id"] for c in self.red.llamadas if c[0].endswith("/api/events")]
        guardados = [e["id"] for e in self.eventos()]
        self.assertEqual(len(guardados), 2)
        self.assertEqual(set(guardados), set(intentos))  # los mismos UUID: reenviar no crea eventos nuevos
        self.assertEqual([e["metric"] for e in self.eventos()], ["Temperatura", "Humedad"])

    def test_08_modo_supabase_arma_la_llamada_a_cuna_ingest(self):
        cfg = self.cfg(MODO="supabase", SUPABASE_URL="https://ejemplo.supabase.co", SUPABASE_KEY="clave-publica-de-prueba")
        self.red = RedSimulada()
        self.correr([(21.0, 55.0)], cfg=cfg)
        envios = [c for c in self.red.llamadas if c[0].endswith("/rest/v1/rpc/cuna_ingest")]
        self.assertEqual(len(envios), 2)
        url, cuerpo, cabeceras = envios[0]
        self.assertEqual(url, "https://ejemplo.supabase.co/rest/v1/rpc/cuna_ingest")
        self.assertEqual(sorted(cuerpo), ["at_time", "device", "event_id", "measurement", "measurement_unit", "metric_name", "secret"])
        self.assertEqual(cuerpo["device"], "CUNA-1")
        self.assertEqual(cabeceras["apikey"], "clave-publica-de-prueba")


class TestAlertaDeExtremoAExtremo(FirmwareBase):
    def test_01_cruzar_el_maximo_deja_una_alerta_en_la_cola(self):
        self.correr([(21.5, 55.0), (22.5, 55.0)])
        cola = self.cola()
        self.assertEqual(len(cola), 1)
        self.assertIn("Temperatura alta: 22.5", cola[0]["message"])

    def test_02_el_cruce_chico_no_espera_al_latido(self):
        """21.9 -> 22.1 cambia solo 0.2 °C, pero cruza el umbral: se envía en esa misma lectura."""
        self.correr([(21.9, 55.0), (22.1, 55.0)])
        self.assertEqual([e["value"] for e in self.eventos("Temperatura")], [21.9, 22.1])
        self.assertEqual(len(self.cola()), 1)

    def test_03_el_limite_exacto_no_alerta(self):
        self.correr([(21.0, 55.0), (22.0, 55.0), (19.0, 55.0)])
        self.assertEqual(self.cola(), [])

    def test_04_bajar_del_minimo_alerta(self):
        self.correr([(20.0, 55.0), (18.5, 55.0)])
        self.assertIn("Temperatura baja: 18.5", self.cola()[0]["message"])

    def test_05_el_dispositivo_usa_los_umbrales_del_servidor_para_enviar_al_instante(self):
        self.admin.call("/api/thresholds", dict(child_id=self.bebe, temp_min=20, temp_max=25))
        self.correr([(24.8, 55.0), (25.1, 55.0)])  # solo 0.3 °C, pero cruza el 25 del servidor
        self.assertEqual([e["value"] for e in self.eventos("Temperatura")], [24.8, 25.1])
        cola = self.cola()
        self.assertEqual(len(cola), 1)
        self.assertIn("Temperatura alta: 25.1", cola[0]["message"])

    def test_06_con_umbrales_distintos_24_8_no_alerta(self):
        self.admin.call("/api/thresholds", dict(child_id=self.bebe, temp_min=20, temp_max=25))
        self.correr([(24.8, 55.0)])
        self.assertEqual(self.cola(), [])

    def test_07_la_humedad_no_dispara_alertas(self):
        self.correr([(21.0, 10.0), (21.0, 99.0)] * 3)
        self.assertEqual(self.cola(), [])

    def test_08_el_endpoint_de_umbrales_exige_la_clave_del_dispositivo(self):
        from apoyo import Browser
        cliente = Browser(self.url)
        sin_clave = cliente.call("/api/device/thresholds", dict(device_id="CUNA-1"))
        self.assertEqual(sin_clave[0], 403)
        mala = cliente.call("/api/device/thresholds", dict(device_id="CUNA-1"), {"Authorization": "Bearer mala"})
        self.assertEqual(mala[0], 403)
        buena = cliente.call("/api/device/thresholds", dict(device_id="CUNA-1"), {"Authorization": "Bearer " + self.keys["CUNA-1"]})
        self.assertEqual(buena, (200, {"temp_min": 19.0, "temp_max": 22.0}))

    def test_09_un_dispositivo_sin_asignar_no_recibe_umbrales(self):
        from apoyo import Browser
        self.keys["LIBRE-1"] = self.change("device", device_id="LIBRE-1", name="Sensor")["secret"]
        codigo, _ = Browser(self.url).call("/api/device/thresholds", dict(device_id="LIBRE-1"),
                                           {"Authorization": "Bearer " + self.keys["LIBRE-1"]})
        self.assertEqual(codigo, 409)
