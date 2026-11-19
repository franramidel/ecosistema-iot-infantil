# Endpoints de auditoría, exportación y retención

Todos responden JSON con `{"error": "..."}` en los fallos, salvo la descarga correcta.

| Método y ruta | Quién | Para qué |
| --- | --- | --- |
| `GET /api/audit` | Administrador | Página del registro unificado. Filtros `child`, `device`, `tipo`, `desde`, `hasta`, `limit` y `after` |
| `GET /api/thresholds?child=` | Cualquier miembro | `{child_id,temp_min,temp_max,default}`. Sin guardar valen 19 y 22 |
| `POST /api/thresholds` | Administrador | Body `{child_id,temp_min,temp_max}`. Rango −20 a 60 y mínimo menor que máximo |
| `POST /api/device/thresholds` | Dispositivo con su clave | Body `{device_id}`. Devuelve `{temp_min,temp_max}` del bebé asignado (409 si no hay) |
| `POST /api/audit/events` | Dispositivo con su clave | Evento propio. El bebé se deduce de la asignación |
| `POST /api/events` | Dispositivo con su clave | Lectura de telemetría. Sin cambios |
| `GET /api/export/xlsx` | Administrador | Descarga el Excel. Filtros `child`, `device`, `desde`, `hasta` |
| `GET /api/report?child=` | Administrador | CSV de lecturas. Sin cambios |

`device` acepta el Device_ID o el UUID del dispositivo.

## `GET /api/export/xlsx`

| Estado | Cuándo |
| --- | --- |
| 200 | Archivo `.xlsx`. `Content-Disposition: attachment` con nombre descriptivo |
| 400 | Fecha con formato o valor inválido, rango invertido, UUID de bebé mal formado, Device_ID inválido |
| 401 | Sin sesión |
| 403 | Cuidador |
| 404 | Bebé o dispositivo inexistente o de otra familia |
| 413 | Más de 1 048 575 filas en una hoja |
| 503 | Falta `openpyxl` |

Ejemplo:

```text
GET /api/export/xlsx?desde=2026-03-01&hasta=2026-03-31
200 OK
Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet
Content-Disposition: attachment; filename="nido-reporte-2026-03-01_2026-03-31-20261118-213705.xlsx"
```

## `GET /api/audit`

Respuesta: `{"items": [...], "next": "<cursor>" | null}`. Cada ítem trae `device_id` y `device_uuid`. Los parámetros internos `hoja` y `corte` no se aceptan por HTTP.

Con Supabase, `next` es el cursor cuando hay más páginas. Solo la exportación lee hasta recibir una página vacía.

## Telegram (SCRUM-24)

Todas exigen sesión. Las POST usan JSON, Origin igual a Host y la cookie `SameSite=Strict`. Funcionan con SQLite y con Supabase. Solo responden 501 si el almacén no implementa Telegram.

| Ruta | Método | Respuesta |
| --- | --- | --- |
| `/api/telegram` | GET | `{vinculado, vinculado_desde, bloqueado, codigo_expira, procesador_activo, ultima_prueba, bot}` |
| `/api/telegram/link` | POST | `{codigo, expira, bot, enlace}`. El código aparece una sola vez |
| `/api/telegram/unlink` | POST | `{}` |
| `/api/telegram/test` | POST | `{id, estado, duplicado, vinculado}`. 409 sin vínculo, 429 si hay otra prueba de hace menos de 15 segundos |

No hay ruta HTTP para encolar alertas. Eso lo hace `solicitar_notificacion` desde el código del servidor.
