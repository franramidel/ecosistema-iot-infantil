# Sprint 2: alcance, pruebas y límites

Las historias se implementaron sobre el código existente. No se rehizo SCRUM-15 a 21 ni 23 a 27, ni se implementó SCRUM-28, 34 ni 53.

| Historia | Qué hay | Prueba principal |
| --- | --- | --- |
| SCRUM-55 Consola de auditoría (solo admin) | Pestaña Auditoría con filtros de bebé, dispositivo y fechas, paginación `after`, exportación a Excel y texto escapado con `esc()` | `test_sprint2.py`, `ui/verificar_sprint2.js` |
| SCRUM-54 Gráfica | Temperatura y humedad con unidades y dos escalas, rangos históricos, umbrales de `/api/thresholds` y actualización en vivo | `test_sprint2.py` (historial), `test_sprint2_nube.py`, UI |
| SCRUM-35 Snapshot de masa | `Masa` en `g` como línea base (100 %) de la sesión de sueño. Sin sesión o sin masa responde 409 y no inventa datos | `test_sprint2.py`, `test_sprint2_nube.py` |
| SCRUM-51 Toma | Cronómetro Start/Stop, duración y volumen en g o ml, guardado como evento `TOMA` en la auditoría, idempotente | `test_sprint2.py`, `test_sprint2_nube.py` |
| SCRUM-57 Ajustes | Umbrales (19 a 22 °C por defecto), velocidad de LED, audio y volumen persistidos. Llamada de voz solo como interfaz. Desarme lógico | `test_sprint2.py`, `test_sprint2_nube.py`, UI |
| SCRUM-22 Búfer sin conexión | `cola.jsonl` en el firmware, hasta 100 eventos, con `event_id` y `captured_at` originales | `test_firmware_cola.py` (14) |
| SCRUM-56 Panel central | Sensor de temperatura, luz, audio y colchón: encendido y manual o programado, persistido | `test_sprint2.py`, `test_sprint2_nube.py` |

## Qué es simulado y qué es real
- Real: el código del servidor, SQLite y el SQL de `009` ejecutado en PostgreSQL 16 local con un sustituto mínimo de los roles de Supabase.
- Simulado: la capa PostgREST y los JWT (`tests/postgres/puente_postgrest.py`), `machine`, `dht`, `network` y `ntptime` del firmware, el reloj del firmware y el navegador (jsdom).
- No verificado: Supabase real, la Pico W real (flash, Wi-Fi, NTP), un navegador real y Windows.

## Límites
- La llamada de voz de esta tabla era un marcador; ahora tiene señalización y WebRTC (ver `SPRINT-2-PERSONA-5.md`).
- Sueño, desarme, módulos y ajustes son estado lógico en la base. Ninguna ruta los envía al hardware, y el firmware aún no los lee.
- El modo "programado" no tiene editor de horarios: solo se guarda el modo.
- Una toma necesita un dispositivo asignado, porque `audit_log` exige dispositivo.
- La protección contra `TOMA` falsificado desde un dispositivo está en el servidor local (`auditoria.py`). El `cuna_record` de Supabase no la tiene.
- Firmware: ver `firmware/cuna-pico/LEEME.md` (pérdida de unos 2 s con corte de energía y sin captura tras reiniciar sin red hasta sincronizar NTP).

## Despliegue
Aplicar `supabase/009_panel_sprint2.sql` en Supabase solo con autorización del equipo. Para las pruebas de nube, definir `PGTEST_URL` hacia un PostgreSQL local desechable.
