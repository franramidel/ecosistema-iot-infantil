# Arquitectura del subsistema de auditoría, retención, exportación y alertas

Cubre SCRUM-20, SCRUM-21, SCRUM-23 y SCRUM-24. No agrega servicios ni cambia la pila: Python estándar, SQLite para la demo y Supabase para la nube.

## Piezas

| Pieza | Archivo | Responsabilidad |
| --- | --- | --- |
| Registro unificado | `cuna/auditoria.py` | Esquema de `audit_log`, validación de entradas, filtros y paginación |
| Migraciones SQLite | `cuna/migraciones.py` | Lleva cualquier base a la versión 4 en una transacción |
| Retención | `cuna/retencion.py`, `respaldo.py` | Triggers de inmutabilidad, verificación, respaldo y restauración |
| Exportación | `cuna/exportar.py` | Libro de Excel por lotes, con protección de fórmulas |
| Almacén local | `cuna/local.py` | `audit_page`, `audit_corte`, `record` sobre SQLite |
| Almacén nube | `cuna/cloud.py` | Los mismos métodos sobre PostgREST y RPC |
| HTTP | `app.py` | `/api/audit`, `/api/audit/events`, `/api/export/xlsx`, `/api/telegram*` |
| Telegram | `cuna/telegram.py`, `cuna/notificaciones.py`, `cuna/procesador.py`, `telegram_worker.py` | Cliente de la API, cola persistente, vinculación y procesador independiente |
| Interfaz | `web/` | Formulario de exportación y tarjeta de Telegram |
| Supabase | `supabase/003` a `007` | Tablas, triggers, RLS, vista de exportación, cola de Telegram |

## Flujo de una lectura

1. El dispositivo envía `POST /api/events` con su clave.
2. El servidor guarda el evento en `events`.
3. Un trigger inserta en `audit_log` un renglón `LECTURA` con el UUID del dispositivo, en la misma transacción.
4. Los triggers de inmutabilidad impiden modificar o borrar ambos renglones.

## Flujo de una exportación

1. El navegador llama `GET /api/export/xlsx` con los filtros.
2. `app.py` valida la sesión y los filtros con `consulta`.
3. `exportar.generar` llama `audit_corte`, que comprueba el rol de administrador y la pertenencia de bebé y dispositivo.
4. Se crea el archivo temporal y se escriben las hojas pidiendo páginas de 500 filas con el corte fijo.
5. `send_file` envía el archivo y `finally` lo borra.

## Flujo de una alerta

1. SCRUM-25 decide que hay una alerta y llama `solicitar_notificacion`.
2. La solicitud se guarda en `notification_queue` y la llamada regresa.
3. `telegram_worker.py`, un proceso aparte, la reclama, la envía por la API de Telegram y cierra el intento.
4. Si la solicitud nació de un evento, se agrega `NOTIFICACION_ENVIADA` o `NOTIFICACION_FALLIDA` a `audit_log`. Esas filas se conservan y salen en el Excel.

Detalle en `SCRUM-24-telegram.md`.

## Decisiones de diseño

- Una sola implementación del exportador para los dos almacenes. Lo que cambia es la consulta, no la lógica del libro.
- Los triggers escriben la auditoría. Ningún código puede guardar una lectura sin su rastro.
- Dos identificadores de dispositivo. Ver `uuid-dispositivos.md`.
- Las reglas de inmutabilidad viven en la base, no en la aplicación.
- La interfaz solo oculta lo que el servidor ya prohíbe.
- El envío de alertas vive en un proceso aparte, para que no dependa del navegador ni del servidor web.
- La cola es una tabla. No se agregan RabbitMQ ni Redis.
