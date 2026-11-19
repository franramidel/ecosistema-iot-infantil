# Integración: cambios, dependencias y aprobaciones pendientes

Este documento es para el equipo. Describe qué agrega la entrega de Ismael en SCRUM-20, 21, 23 y 24, qué toca de otros módulos y qué falta aprobar.

> **Estado: no está lista para fusionarse en `main`.** Hay cambios al modelo compartido que esperan aprobación de Sebastián. Ninguna migración de Supabase se ejecutó en un Supabase real, ni en el compartido ni en uno de pruebas. Telegram nunca se probó con un bot real.

## 1. Qué es independiente y qué no

Hay dos grupos de cambios. No son independientes entre sí.

**Cambios de integración.** Código, rutas, interfaz y pruebas de las cuatro historias que se conectan con el sistema existente sin cambiar su modelo de datos.

**Cambios al modelo compartido.** Cambios a `devices`, `events` y `assignments`, que son de Sebastián.

No se pueden fusionar por separado hoy. La razón es técnica:

- `audit_log.device_uuid` es una clave foránea real hacia `devices.uuid`. La tabla de auditoría de SCRUM-20 ya la lleva. Sin la columna `devices.uuid`, la auditoría actual no funciona.
- La exportación de SCRUM-23 y la vista `audit_export` leen `device_uuid`.
- `supabase/007_telegram.sql` referencia `audit_log`.
- `cuna/local.py` crea `devices` desde una migración que agrega `uuid`.

Separarlos exigiría rediseñar `audit_log` sin `device_uuid`. Eso no está aprobado y no se hizo. Se decidió conservar el diseño de doble identificador:

| Campo | Qué es |
| --- | --- |
| `devices.id` | Identificador de texto operativo que ya usa el proyecto, por ejemplo `TEMP-01`. No se cambió |
| `devices.uuid` | UUID persistente y único, agregado |
| `audit_log.device_uuid` | Clave foránea real hacia `devices.uuid` |

Este diseño está **pendiente de aprobación de Sebastián**. Mientras no la dé, nada de lo que depende de él es seguro para desplegar.

## 2. Cambios al modelo compartido

| # | Cambio | Archivos | Quién revisa |
| --- | --- | --- | --- |
| C1 | Columna `uuid` en `devices`, claves únicas `(uuid)` y `(id, uuid)`, y trigger que impide cambiarla. No toca `devices.id` | `supabase/005_device_uuid.sql`, `cuna/migraciones.py` | Sebastián |
| C2 | En SQLite, `devices` sale del `SCHEMA` base de `local.py` y la crea la migración. Se reconstruye la tabla una sola vez en bases antiguas, con verificación de conteo | `cuna/local.py`, `cuna/migraciones.py` | Sebastián |
| C3 | Triggers `after insert` en `events` y `assignments`, y carga inicial desde ellas hacia `audit_log` | `supabase/003_auditoria.sql`, `cuna/auditoria.py` | Sebastián y Francisco |
| C4 | Triggers que impiden vaciar `events`, `audit_log` y `assignments`, y borrar filas de `assignments` | `supabase/004_retencion.sql`, `cuna/retencion.py` | Sebastián |
| C5 | Dependencia nueva `openpyxl` | `requirements.txt` | Todo el equipo |
| C6 | Al arrancar, la migración de SQLite modifica `datos/demo.sqlite3`, que está versionado en el repositorio base | `cuna/migraciones.py` | Todo el equipo |

El código actual del equipo no borra filas de `assignments` ni vacía `events`. Un módulo futuro que lo intente fallaría a propósito por C4.

## 3. Cambios en archivos de otros integrantes

Todos son de Sebastián, según el autor del commit base. Casi todo es código agregado. Las únicas líneas existentes que cambiaron son las de la última columna.

| Archivo | Qué se agregó | Líneas existentes tocadas |
| --- | --- | --- |
| `app.py` | Rutas `/api/audit`, `/api/audit/events`, `/api/export/xlsx`, `/api/telegram*`, `send_file`, `device_key`, `bot_username` | Ninguna. `response()` y la autenticación de `/api/events` quedaron como estaban |
| `cuna/cloud.py` | Métodos nuevos al final | `request()`: la línea que mapea `PT409` ahora también mapea `PT404` y `PT429`. Es un cambio compartido por todo el cliente |
| `cuna/local.py` | Métodos nuevos al final | Se quitó `devices` del `SCHEMA` y se reemplazó `executescript(SCHEMA)` por `migraciones.migrar`. El `insert into devices` nombra columnas. Todo esto es C2 |
| `web/index.html` | Dos tarjetas, exportación y Telegram | La etiqueta de cierre del panel se movió al final. El botón de CSV no cambió |
| `web/app.js`, `web/estilo.css` | Código y estilos de esas dos tarjetas | Ninguna |
| `README.md` | Un párrafo de Telegram al final de "Dónde extender" | Ninguna. La frase original de Sebastián se restauró tal cual |
| `.gitignore` | Reglas para respaldos y copias previas | Ninguna |

`send_file` repite los encabezados de seguridad de `response()` en lugar de modificarlo. Si Sebastián cambia esos encabezados, hay que cambiarlos también ahí.

## 4. Aprobaciones pendientes

| Persona | Qué debe responder o aprobar |
| --- | --- |
| Sebastián | 1. ¿El Supabase desplegado coincide con `001_usuarios.sql` y `002_primer_administrador.sql`? No se verificó. 2. C1 y C2: `devices.uuid` y la nueva forma de crear `devices` en SQLite. 3. C3 y C4: triggers sobre `events` y `assignments`. 4. El cambio en `request()` de `cloud.py`. 5. Si su "vinculación de cuentas" incluye Telegram. Mi `telegram_links` es una tabla propia y no toca `members` |
| Francisco | 1. Cómo llamará a `solicitar_notificacion`. 2. Si registrará las alertas en `audit_log`, que es lo que supone `notification_queue.event_id`. 3. Cómo formará la clave de idempotencia. 4. Qué estados usa. Ver sección 5. 5. C3, porque los triggers sobre `events` le afectan |
| Jan Carlo | Si su consola de auditoría usará `GET /api/audit`. No hay interfaz de consola en esta entrega |
| Todo el equipo | C5, `openpyxl`. C6, la modificación de `datos/demo.sqlite3` |

## 5. Contrato con SCRUM-25

Es una propuesta. Francisco no la ha aceptado.

```
solicitar_notificacion(usuario_id, clave_idempotencia, *, titulo, dispositivo=None,
                       ocurrido=None, estado="Advertencia", evento_id=None)
```

Devuelve `{id, estado, duplicado, vinculado}`. Estados válidos: `Informativo`, `Advertencia`, `Crítico`. La función guarda la solicitud y vuelve sin esperar a Telegram. Es interna: la debe llamar código del servidor, no el navegador ni un dispositivo.

Detalle en `SCRUM-24-telegram.md`. Esta entrega no detecta eventos críticos ni decide cuándo alertar.

## 6. Qué se verificó y qué no

| Etiqueta | Qué cubre |
| --- | --- |
| Verificado | Las pruebas automáticas en SQLite y en PostgreSQL 16 local. Ver `pruebas.md` |
| Simulado | Telegram, el puente a PostgREST de `tests/puente_postgrest.py`, el sustituto de `auth` y los roles de Supabase |
| Supuesto | Que la clave secreta de Supabase se comporta como `service_role` ante PostgREST. Que PostgREST traduce `PT404` y `PT429` igual que `PT409` |
| Pendiente | Telegram real. Supabase real. Contrato con SCRUM-25. Las aprobaciones de la sección 4 |
| Fuera de alcance | Detección y disparo de alertas, autenticación, usuarios, roles, bebés, dispositivos y consola de auditoría |

## 7. Protecciones de esta entrega

- Los escenarios SQL, el sustituto de Supabase y los scripts de prueba se niegan a correr si no están en un entorno local desechable. Los scripts rechazan URL de Supabase.
- `solicitar_alerta.py --supabase` pide confirmación explícita antes de escribir y no imprime claves.
- Las migraciones 003 a 007 llevan un aviso de que no están aprobadas para desplegar.
- Ningún archivo contiene tokens ni claves reales. Los valores de prueba son sintéticos.


## 8. Alertas de temperatura (Persona 3)
Añade `008_alertas_temperatura.sql` (no aprobada para desplegar), `cuna/alertas.py`, endpoints `/api/thresholds` y `/api/device/thresholds`, la tarjeta "Umbrales de temperatura" en la web y `firmware/cuna-pico`. Toca `local.py` (`ingest` llama a `alertas.evaluar`), `migraciones.py`, `cloud.py` y `app.py`. Depende de SCRUM-24 (cola de Telegram) y SCRUM-20 (auditoría). Detalle en `SCRUM-25-alertas-temperatura.md`.
