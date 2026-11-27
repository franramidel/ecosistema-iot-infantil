# Sprint 2 · Persona 5: alimentación, voz y fotos (parte de software)

Alcance: SCRUM-50, SCRUM-52, SCRUM-31 parte B y SCRUM-43 parte B. SCRUM-49 (juego de botones offline) es solo hardware y no está aquí.
Todo funciona en la demo local (SQLite). Con `--supabase` estas rutas responden 501: falta el adaptador de nube (ver Límites).

## Qué hay
| Historia | RF | Dónde | Prueba |
| --- | --- | --- | --- |
| SCRUM-50 Temperatura del biberón | RF-ALI-01 | `cuna/alimentacion.py`, hook en `Local.ingest`, pestaña **Alimentación** | `TestBiberon` |
| SCRUM-52 Recordatorio | RF-ALI-03 | `cuna/alimentacion.py` (`disparar`), `Procesador.procesar_cola`, pestaña **Alimentación** | `TestRecordatorios` |
| SCRUM-43 B Almacenamiento y galería | RF-SEG-02, RNF-SEG-03/04 | `cuna/fotos.py`, `POST /api/photos`, pestaña **Fotos** | `TestFotos` |
| SCRUM-31 B Voz bidireccional | RF-CUN-06, RNF-REN-02 | `cuna/voz.py`, `/api/voice/*`, `web/p5.js`, `web/cuna-voz.*` | `TestVoz` |

Pruebas: `python -m unittest discover -s tests -p "test_persona5.py" -v` (39 casos).

## SCRUM-50 · Biberón
* El dispositivo envía `POST /api/events` con `metric: "TemperaturaBiberon"`, `unit: "°C"` (el simulador ya lo admite: `python simular.py --device CUNA-1 --medida TemperaturaBiberon --valor 41`).
* Rango por defecto 36 a 40 °C, objetivo 37 °C, configurable por bebé (solo administrador). Alerta si **supera** 40 («muy caliente», severidad crítica) o **baja** de 36 («muy frío», advertencia). Los límites exactos no alertan.
* Se avisa solo al cambiar de estado. Auditoría `ALERTA_BIBERON_CALIENTE` / `ALERTA_BIBERON_FRIO` + cola de Telegram, en la misma transacción que la lectura. Reintentar con el mismo `event_id` no duplica. Una lectura atrasada no deshace un estado más reciente.
* Lecturas fuera de 0 a 100 °C se rechazan (error de sensor). Alerta **visual** en la app: estado con texto y color, y un aviso con `role="alert"`.

## SCRUM-52 · Recordatorios
* `GET/POST /api/reminders`. Por intervalo (15 min a 24 h) o por horas fijas (HH:MM, hora de Costa Rica), hasta 10 por bebé. Crear, activar/desactivar y borrar requieren permiso de operar.
* Los dispara el procesador de Telegram: **hay que tener corriendo `python telegram_worker.py`**. La clave de la cola incluye el vencimiento, así que no se duplican. Tras un apagón largo se envía un solo aviso.
* El mensaje no lleva el nombre del bebé (mismo criterio que las demás alertas).

## SCRUM-43 B · Fotos
* La cámara sube con `POST /api/photos`: cuerpo = imagen cruda; encabezados `Authorization: Bearer <clave>`, `X-Device-Id`, `X-Event-Id` (UUID, idempotencia), `X-Captured-At`, `Content-Type`. Máximo 5 MB, solo JPEG/PNG; el tipo se comprueba con los bytes, no con lo declarado. Demo: `python simular_foto.py --device KIT-01`.
* El bebé sale de la asignación vigente en la fecha de captura (409 si no había). Cada foto queda en auditoría como `FOTO_SEGURIDAD` con la ruta del objeto (`fotos/<uuid>.jpg`), no con una URL.
* Almacén privado en `datos/fotos/` (ignorado por git). Fotos inmutables (triggers), igual que la retención del resto.
* `GET /api/photos?child=` lista con paginación (`limit`, `before`) y devuelve URL **firmadas (HMAC) que vencen a los 5 minutos**. `GET /api/photo` exige además la sesión de la familia. Otra familia recibe 404 aunque tenga la URL. `CUNA_PHOTO_SECRET` fija la clave de firma; si no, se genera al arrancar (las URL vigentes se invalidan al reiniciar).
* Para la **notificación silenciosa** (Persona 2) y la alerta de apertura (Persona 4): el insumo es `fotos.url_firmada(secreto, photo_id, ahora_ts)`.

## SCRUM-31 B · Voz
* WebRTC directo entre el navegador de la app y la cuna; el servidor solo hace **señalización** (`POST /api/voice/signal`, `POST /api/voice/poll` con espera larga). Mensajes en memoria, con vida de 45 s. Una llamada por bebé (409 si está ocupada). Exige permiso de operar.
* Una cuna real usa la clave del dispositivo (`device_id` en el cuerpo, rol `cuna`). Para probar sin hardware: abre `/cuna-voz` en otra pestaña o dispositivo con sesión de la misma familia y pulsa «Poner la cuna a la escucha»; luego **Control → Llamada de voz**.
* La app muestra el retardo de red estimado (RTT/2) contra la meta de 500 ms (RNF-REN-02). No incluye captura ni reproducción.
* STUN/TURN opcional con `CUNA_ICE_SERVERS=stun:host:3478,...`. Sin él funciona en red local.

## Límites (qué no está verificado)
* **Voz y galería no se probaron en un navegador real** (aquí no hay navegador ni WebRTC). Las pruebas cubren servidor, señalización, permisos y firmas. Falta una prueba manual: dos pestañas, micrófono, y medir el retardo.
* `getUserMedia` solo funciona en `localhost` o HTTPS: desde otro dispositivo de la red hace falta HTTPS.
* Sin Supabase: tablas (`bottle_thresholds`, `bottle_state`, `feed_reminders`, `photos`), RLS, funciones y Supabase Storage con URL firmadas no están escritos; el almacén local es la interfaz que se cambiaría.
* La voz no se registra en la auditoría. Los recordatorios disparados tampoco (quedan en la cola de Telegram).
* El juego offline (SCRUM-49), el audio en firmware de la cuna (SCRUM-31 A) y la captura de la cámara (SCRUM-43 A) son hardware.
