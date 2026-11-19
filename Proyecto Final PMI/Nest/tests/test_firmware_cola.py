"""SCRUM-22 (RF-DAT-03): búfer de eventos ante fallas de conexión del firmware de la Pico W.

Igual que test_firmware.py: la placa no existe aquí. `machine`, `dht`, `network` y `ntptime` son sustitutos, el reloj
es simulado y el "reinicio" es cargar main.py de nuevo con el mismo archivo de cola. Lo real es el código de main.py,
el servidor y la base SQLite. NO prueban Wi-Fi, flash ni MicroPython reales: eso se verifica con la placa.
"""
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cuna.common import instant, uid
from test_firmware import FirmwareBase, Red, Reloj, Sensor, cargar_firmware, ejecutar

# Cada lectura cambia 0.6 °C respecto a la anterior: en cada ciclo se generan eventos nuevos (temperatura y humedad).
ZIGZAG = [(21.0 + (0.6 if i % 2 else 0.0), 55.0) for i in range(60)]


class RedConCorte(Red):
    """Sin red mientras el reloj simulado no llegue a `reconecta_ms`; después, el servidor real."""

    def __init__(self, reloj, reconecta_ms):
        super().__init__()
        self.reloj, self.reconecta_ms = reloj, reconecta_ms

    def post(self, url, data=None, headers=None):
        if self.reloj.ms < self.reconecta_ms:
            self.llamadas.append((url, json.loads(data), dict(headers or {})))
            raise OSError("sin red")
        return super().post(url, data, headers)


class ColaBase(FirmwareBase):
    def setUp(self):
        super().setUp()
        self.carpeta = tempfile.TemporaryDirectory()
        self.addCleanup(self.carpeta.cleanup)
        self.archivo = Path(self.carpeta.name) / "cola.jsonl"
        self.hace_una_hora = datetime.now(timezone.utc) - timedelta(hours=1)
        self.generados = []

    def vida(self, lecturas, ciclos, red, reloj=None, pasado=False, maximo=None):
        """Una 'vida' de la placa. Con `pasado` el reloj del dispositivo marca desde hace una hora."""
        reloj = reloj or Reloj()
        fw = cargar_firmware(Sensor(lecturas), reloj)
        fw.COLA_ARCHIVO = str(self.archivo)
        if maximo:
            fw.MAX_PENDIENTES = maximo
        if pasado:
            base = self.hace_una_hora
            fw.ahora_iso = lambda: (base + timedelta(milliseconds=reloj.ms)).isoformat(timespec="microseconds")
        original = fw.eventos_de_lectura

        def espia(*args):
            eventos = original(*args)
            self.generados.extend(dict(e) for e in eventos)
            return eventos

        fw.eventos_de_lectura = espia
        ejecutar(fw, self.cfg(), red, ciclos)
        return fw

    def en_archivo(self):
        if not self.archivo.exists():
            return []
        return [json.loads(linea) for linea in self.archivo.read_text().splitlines() if linea.strip()]

    def guardados(self):
        return [(e["id"], instant(e["captured_at"]), e["metric"], e["value"], e["unit"]) for e in self.eventos()]

    @staticmethod
    def clave(e):
        return (e["event_id"], instant(e["captured_at"]), e["metric"], e["value"], e["unit"])

    def esperados(self, eventos=None):
        return sorted(self.clave(e) for e in (self.generados if eventos is None else eventos))


class TestColaSinConexion(ColaBase):
    def test_01_sin_red_acumula_y_al_volver_envia_todo_en_orden_con_sus_datos_originales(self):
        reloj = Reloj()
        red = RedConCorte(reloj, reconecta_ms=7000)  # los ciclos de 0, 2, 4 y 6 s no tienen red
        self.vida(ZIGZAG, 8, red, reloj=reloj, pasado=True)
        self.assertEqual(len(self.generados), 16)
        self.assertEqual(sorted(self.guardados()), self.esperados())  # mismos event_id, captured_at, valor y unidad
        envios = [c[1]["event_id"] for c in red.llamadas if c[0].endswith("/api/events") and c[1]["event_id"] in {e["id"] for e in self.eventos()}]
        orden_servidor = [e["id"] for e in self.eventos()]
        self.assertEqual(sorted(orden_servidor), sorted(set(envios)))
        # La primera captura sigue marcada con la hora de hace una hora, no con la del reenvío.
        mas_viejo = min(datetime.fromisoformat(e["captured_at"]) for e in self.eventos())
        self.assertLess(mas_viejo, datetime.now(timezone.utc) - timedelta(minutes=55))
        self.assertEqual(self.en_archivo(), [])  # cola vacía: el archivo se borra

    def test_02_el_reenvio_respeta_el_orden_de_captura(self):
        reloj = Reloj()
        self.vida(ZIGZAG, 8, RedConCorte(reloj, 7000), reloj=reloj, pasado=True)
        orden = [c for c in self.generados]
        capturas = [e["captured_at"] for e in self.eventos("Temperatura")]
        self.assertEqual(capturas, sorted(capturas))
        self.assertEqual(len(set(capturas)), len(orden) // 2)  # cada ciclo con su propia hora, ninguna repetida

    def test_03_mientras_hay_red_nunca_se_escribe_el_archivo(self):
        self.vida(ZIGZAG, 10, Red())
        self.assertEqual(len(self.eventos("Temperatura")), 10)
        self.assertFalse(self.archivo.exists())

    def test_04_sin_red_el_archivo_guarda_exactamente_lo_pendiente_sin_repetidos(self):
        reloj = Reloj()
        self.vida(ZIGZAG, 5, RedConCorte(reloj, reconecta_ms=10 ** 9), reloj=reloj, pasado=True)
        guardados = self.en_archivo()
        self.assertEqual(len(guardados), 10)
        self.assertEqual(sorted(self.clave(e) for e in guardados), self.esperados())
        self.assertEqual(self.eventos(), [])  # nada llegó al servidor


class TestColaSobreviveAlReinicio(ColaBase):
    def test_01_los_eventos_pendientes_se_envian_tras_reiniciar_con_su_event_id_y_captured_at(self):
        reloj = Reloj()
        self.vida(ZIGZAG, 4, RedConCorte(reloj, reconecta_ms=10 ** 9), reloj=reloj, pasado=True)  # vida 1: sin red
        antes = self.esperados()
        self.assertEqual(len(antes), 8)
        self.assertEqual(self.eventos(), [])
        # vida 2: la placa arranca de nuevo (memoria vacía) y esta vez hay red
        self.vida([(21.0, 55.0)], 1, Red())
        guardados = self.guardados()
        for clave in antes:
            self.assertIn(clave, guardados)  # mismo event_id y mismo captured_at de antes del reinicio
        self.assertEqual(len(guardados), 8 + 2)  # los 8 de antes y los 2 del primer ciclo nuevo; ningún duplicado
        self.assertEqual(len({g[0] for g in guardados}), len(guardados))
        self.assertFalse(self.archivo.exists())

    def test_02_reiniciar_dos_veces_sin_red_no_pierde_ni_duplica(self):
        reloj = Reloj()
        self.vida(ZIGZAG, 3, RedConCorte(reloj, 10 ** 9), reloj=reloj, pasado=True)
        self.vida([(21.0, 55.0)], 0, RedConCorte(Reloj(), 10 ** 9))  # reinicio sin tiempo de captar nada
        primera = self.en_archivo()
        self.assertEqual(sorted(self.clave(e) for e in primera), self.esperados())
        self.vida([(21.0, 55.0)], 1, Red())
        self.assertEqual(len(self.eventos()), 6 + 2)

    def test_03_reenviar_lo_que_ya_llego_no_crea_duplicados(self):
        """Corte de energía justo después de enviar: la cola aún lo tiene y lo reenvía con el mismo event_id."""
        reloj = Reloj()
        self.vida(ZIGZAG, 3, RedConCorte(reloj, 10 ** 9), reloj=reloj, pasado=True)
        eventos = self.en_archivo()
        for ev in eventos[:2]:  # estos dos ya habían llegado al servidor
            codigo, cuerpo = self.send(ev, device="CUNA-1")
            self.assertEqual(codigo, 200, cuerpo)
        antes = len(self.eventos())
        self.vida([(21.0, 55.0)], 0, Red())
        self.vida([(21.0, 55.0)], 1, Red())
        self.assertEqual(antes, 2)
        self.assertEqual(len({g[0] for g in self.guardados()}), len(self.guardados()))
        self.assertEqual(len(self.guardados()), 6 + 2)


class TestColaLimiteYArchivo(ColaBase):
    def test_01_al_llenarse_se_pierden_primero_los_mas_viejos_y_el_archivo_los_refleja(self):
        reloj = Reloj()
        self.vida(ZIGZAG, 10, RedConCorte(reloj, 10 ** 9), reloj=reloj, pasado=True, maximo=6)
        self.assertEqual(len(self.generados), 20)
        ultimos = self.esperados(self.generados[-6:])
        viejos = {e["event_id"] for e in self.generados[:-6]}
        self.assertEqual(sorted(self.clave(e) for e in self.en_archivo()), ultimos)
        self.vida([(21.0, 55.0)], 0, Red())  # tras el reinicio el tope vuelve a 100: lo que quedó en el archivo cabe
        self.vida([(21.0, 55.0)], 1, Red())
        guardados = self.guardados()
        for clave in ultimos:
            self.assertIn(clave, guardados)
        self.assertTrue(viejos.isdisjoint({g[0] for g in guardados}))

    def test_02_el_limite_por_defecto_ya_no_es_de_20(self):
        fw = cargar_firmware(Sensor([(21.0, 55.0)]), Reloj())
        self.assertEqual(fw.MAX_PENDIENTES, 100)
        reloj = Reloj()
        self.vida(ZIGZAG, 30, RedConCorte(reloj, 10 ** 9), reloj=reloj, pasado=True)
        self.assertEqual(len(self.en_archivo()), 60)  # 60 eventos pendientes: la cola de antes habría perdido 40

    def test_03_el_archivo_no_crece_sin_limite_y_nunca_pierde_eventos_vigentes(self):
        reloj = Reloj()
        self.vida(ZIGZAG * 2, 60, RedConCorte(reloj, 10 ** 9), reloj=reloj, pasado=True, maximo=10)
        self.assertLessEqual(len(self.archivo.read_text().splitlines()), 20)
        self.assertEqual(sorted(self.clave(e) for e in self.en_archivo()), self.esperados(self.generados[-10:]))

    def test_04_lineas_cortadas_basura_y_repetidos_se_ignoran_y_se_compacta(self):
        reloj = Reloj()
        self.vida(ZIGZAG, 2, RedConCorte(reloj, 10 ** 9), reloj=reloj, pasado=True)
        buenos = self.en_archivo()
        with open(self.archivo, "a") as f:
            f.write(json.dumps(buenos[0]) + "\n")        # repetido
            f.write("esto no es json\n")                   # basura
            f.write('{"event_id": "x"}\n')                 # evento incompleto
            f.write(json.dumps(buenos[1])[:25])            # línea cortada por un corte de energía
        self.vida([(21.0, 55.0)], 1, Red())
        self.assertEqual(len(self.guardados()), 4 + 2)
        self.assertEqual(len({g[0] for g in self.guardados()}), 6)
        self.assertFalse(self.archivo.exists())

    def test_05_si_falta_el_archivo_pero_quedo_el_temporal_de_una_reescritura_se_recupera(self):
        reloj = Reloj()
        self.vida(ZIGZAG, 2, RedConCorte(reloj, 10 ** 9), reloj=reloj, pasado=True)
        self.archivo.rename(str(self.archivo) + ".tmp")  # corte entre borrar el archivo y renombrar el temporal
        self.vida([(21.0, 55.0)], 1, Red())
        self.assertEqual(len(self.guardados()), 4 + 2)
        self.assertFalse(self.archivo.exists())
        self.assertFalse(Path(str(self.archivo) + ".tmp").exists())

    def test_06_un_evento_que_el_servidor_rechaza_para_siempre_se_descarta_sin_bloquear_la_cola(self):
        malo = dict(event_id=uid(), device_id="CUNA-1", captured_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="microseconds"),
                    metric="Temperatura", value=21.0, unit="°C")
        bueno = dict(event_id=uid(), device_id="CUNA-1", captured_at=instant(self.hace_una_hora.isoformat()), metric="Temperatura", value=20.0, unit="°C")
        self.archivo.write_text(json.dumps(malo) + "\n" + json.dumps(bueno) + "\n")
        self.vida([(21.0, 55.0)], 0, Red())
        self.vida([(21.0, 55.0)], 1, Red())
        ids = {g[0] for g in self.guardados()}
        self.assertIn(bueno["event_id"], ids)
        self.assertNotIn(malo["event_id"], ids)
        self.assertFalse(self.archivo.exists())

    def test_07_un_fallo_de_escritura_no_detiene_al_dispositivo(self):
        reloj = Reloj()
        fw = cargar_firmware(Sensor(ZIGZAG[:3]), reloj)
        fw.COLA_ARCHIVO = str(Path(self.carpeta.name) / "no-existe" / "cola.jsonl")  # carpeta inexistente
        ejecutar(fw, self.cfg(), RedConCorte(reloj, 10 ** 9), 3)  # no debe lanzar excepción
        self.assertEqual(self.eventos(), [])
