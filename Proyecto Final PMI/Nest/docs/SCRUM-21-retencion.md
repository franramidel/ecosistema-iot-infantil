# SCRUM-21 · RF-DAT-02 · Retención permanente

Autor: Ismael M. Rama: `feature/ismael-auditoria-exportacion-telegram`. Esta historia se apoya en SCRUM-20.

## Qué es la retención permanente

Es la regla de que ningún registro de telemetría se borra por tiempo ni por limpieza automática. Un padre puede abrir el historial de su bebé dentro de un mes o de dos años y encontrar las mismas lecturas. No significa que los datos nunca se puedan perder. Un disco puede fallar, un archivo puede dañarse y una persona puede borrar a mano. Para eso existen los respaldos.

## Cómo funciona en el proyecto

Casi todo ya estaba hecho antes de esta historia. Se revisó y se comprobó, y solo se programó lo que faltaba.

Lo que ya existía: la tabla `events` tiene triggers que impiden UPDATE y DELETE. No hay ningún proceso, hilo, tarea programada ni consulta que borre telemetría. El cliente web no usa `localStorage`, `sessionStorage` ni `indexedDB`, y el servidor responde con `Cache-Control: no-store`. La base de datos guarda todo en el servidor.

Lo que se agregó:
- SQLite: un trigger que impide borrar filas de `assignments`. Sin esas filas, las lecturas guardadas quedarían sin explicación de a qué bebé pertenecen.
- Supabase: la migración `004_retencion.sql` bloquea `TRUNCATE` sobre `events`, `audit_log` y `assignments`, y también el borrado de asignaciones. `TRUNCATE` vacía una tabla sin activar los triggers de fila, por lo que antes era un hueco real. La prueba confirmó que también se bloquea cuando llega en cascada desde `families`.
- `cuna/retencion.py` y `respaldo.py`: respaldo en caliente, verificación y restauración de la base SQLite.
- `supabase/verificar_retencion.sql`: diez comprobaciones de solo lectura. La décima, agregada con el UUID, revisa que ningún dispositivo carezca de UUID y que la auditoría no lo contradiga que se pueden ejecutar en el proyecto real de Supabase.

## Diferencias entre SQLite y Supabase

| Tema | SQLite, demo | Supabase, nube |
| --- | --- | --- |
| Dónde viven los datos | Un archivo, `datos/demo.sqlite3` | PostgreSQL administrado por Supabase |
| Inmutabilidad | Triggers `keep_event`, `keep_history`, `audit_keep_*`, `keep_assignments` | Triggers `immutable_event`, `audit_immutable`, `keep_assignments`, más `no_truncate` |
| Permisos | Una sola aplicación abre el archivo | RLS y solo SELECT para los roles de la API |
| Respaldo | `respaldo.py`, copia en línea de SQLite | Respaldos diarios de Supabase en planes Pro o superiores. En el plan gratuito, `pg_dump` manual |
| Restauración | `respaldo.py restaurar`, con el servidor detenido | Panel de Supabase, o `pg_restore` en un proyecto nuevo |
| Quién puede saltarse la protección | Quien tenga acceso al archivo | El propietario de la base, deshabilitando triggers |

## Qué pasa después de un reinicio

Los datos están en el archivo o en la base remota, no en la memoria del servidor. Lo único que se pierde al reiniciar son las sesiones abiertas, que viven en memoria, así que todos deben iniciar sesión otra vez. Al arrancar, la demo vuelve a ejecutar el esquema, que es idempotente, y el relleno de auditoría, que no duplica nada. Hay una prueba que reinicia el servidor tres veces y compara el contenido completo de las siete tablas.

## Mecanismos que protegen la información

La inmutabilidad en la base impide que un error de programación, o un compañero que escriba código nuevo, borre historial. Las claves foráneas impiden borrar un bebé o un dispositivo que tenga lecturas. Los permisos de Supabase dan a la API solo lectura sobre el historial. La verificación `verificar_retencion.sql` y `respaldo.py verificar` detectan cuando alguna de esas protecciones falta.

## Respaldos

**SQLite.** `python respaldo.py crear` usa la API de copia en línea de SQLite. Es consistente aunque el servidor esté escribiendo. Primero escribe en un archivo `.parcial`, lo verifica y recién entonces lo renombra. Nunca sobrescribe un respaldo existente y nunca borra respaldos viejos, porque depurar copias de datos de menores es una decisión humana. El archivo queda con permisos 0600 donde el sistema los respeta.

`python respaldo.py restaurar ARCHIVO --confirmar` verifica el respaldo antes de tocar nada. Si la base actual existe, la copia a un archivo `.antes-de-restaurar-FECHA`. Se niega a restaurar si detecta la base en uso. Si algo falla, el destino queda intacto. Hay pruebas con archivos de basura, cortados, vacíos y con una base ajena.

**Supabase.** Lo que ofrece depende del plan. Según la documentación oficial, los respaldos diarios automáticos existen en los planes Pro, Team y Enterprise, con 7, 14 y hasta 30 días. Point-in-Time Recovery es un complemento de pago. El plan gratuito no aparece con respaldos automáticos, y la documentación recomienda exportar con `supabase db dump` y guardar copias fuera de Supabase. Los respaldos físicos no se pueden descargar directamente. Los respaldos no incluyen archivos de Storage y no guardan las contraseñas de roles personalizados. Durante una restauración el proyecto no está disponible.

**El plan real del proyecto es desconocido.** Hay que revisarlo en el panel antes de afirmar que existen respaldos. Si es gratuito, la estrategia mínima es un `pg_dump` manual y periódico:

```powershell
$env:SUPABASE_DB_URL = "<cadena de conexión del panel, sin guardarla en ningún archivo>"
pg_dump --format=custom --no-owner $env:SUPABASE_DB_URL -f "respaldo-$(Get-Date -Format yyyyMMdd).dump"
pg_restore --list respaldo-20261118.dump
```

`pg_restore --list` confirma que el archivo se puede leer. Para restaurar, usa un proyecto nuevo y una base vacía, nunca la de producción.

## Cómo se comprueba la integridad

`verificar` revisa seis cosas: `integrity_check` de SQLite, claves foráneas sin huérfanos, presencia de las siete tablas, presencia de los cinco triggers de inmutabilidad, conteos por tabla, y que toda lectura tenga su renglón de auditoría. Se ejecuta solo sobre cada respaldo y sobre cada restauración. En Supabase, `verificar_retencion.sql` hace el equivalente y suma que no haya tareas `pg_cron` y que los roles de la API solo tengan SELECT.

## Limitaciones técnicas

Retención indefinida no es disponibilidad perfecta. Si el disco o el proyecto de Supabase se pierden sin un respaldo reciente, los datos se pierden. Las inmutabilidades no protegen contra un administrador con acceso total, que puede deshabilitar triggers o borrar el archivo. No hay respaldo automático en la demo. Programarlo en Windows es opcional con el Programador de tareas. No se incluyó un programador dentro del servidor para no agregar procesos en segundo plano. La demo no cifra el archivo ni los respaldos.

El panel muestra las últimas 100 lecturas del bebé. El historial completo se consulta con `GET /api/audit?tipo=LECTURA` paginado, y con el CSV de `/api/report`. Ampliar el panel con paginación es trabajo del frontend de la Persona 1.

El servidor responde 500 ante un error de almacenamiento y revierte la transacción. Nada se guarda a medias. El dispositivo debe reintentar con el mismo `event_id`. El búfer que lo hace automáticamente es SCRUM-22 y no se implementó aquí.

## Limitaciones legales

El proyecto guarda datos de menores. En Costa Rica, la Ley 8968 reconoce a la persona derechos de acceso, rectificación y supresión de sus datos personales. Esto es una referencia y no asesoría legal. Un sistema que nunca pueda borrar nada chocaría con ese derecho, así que la inmutabilidad se diseñó para impedir borrados accidentales y automáticos, no los autorizados. Los respaldos son otra copia de los mismos datos y también deben borrarse o reemplazarse cuando se atienda una solicitud.

### Procedimiento excepcional de supresión

Solo lo hace el administrador de la base, con una solicitud verificada, y debe quedar anotado fuera del sistema. No hay un script para evitar un uso accidental.

SQLite: detener el servidor, hacer un respaldo, y con una conexión directa borrar los triggers `keep_event`, `keep_history`, `keep_assignments`, `audit_keep_update` y `audit_keep_delete`. Después eliminar las filas del bebé en este orden: `audit_log`, `events`, `assignments`, y luego el bebé y los dispositivos que ya no se usen. Al volver a iniciar el servidor, el esquema recrea los triggers. Hay una prueba que ejecuta este procedimiento y comprueba que la base queda sana.

Supabase: en una transacción, `alter table ... disable trigger` sobre las tablas del historial, los mismos borrados en el mismo orden, y `enable trigger`. Esto requiere el rol propietario. No se ejecutó contra un proyecto real.

## Relación con SCRUM-20

SCRUM-20 creó la bitácora y la dejó inmutable. SCRUM-21 garantiza que esa bitácora y la telemetría sobrevivan en el tiempo, y que se puedan recuperar. No duplica nada de la lógica de auditoría. Reutiliza `audit_log`, `verificar` revisa la coherencia entre `events` y `audit_log`, y `GET /api/audit` es la consulta histórica. SCRUM-23 exportará desde la misma tabla.

## Estado de los criterios de aceptación

| Criterio | Estado |
| --- | --- |
| CA1. Ningún proceso borra ni depura telemetría | Cumplido en SQLite y probado. En Supabase, probado en PostgreSQL local. No ejecutado contra el proyecto real |
| CA2. El cliente no guarda históricos | Cumplido. Revisión estática de `web/` y encabezado `no-store` probados |

## Resultados observados

La cifra de abajo es la de la entrega de SCRUM-21. Las pruebas vigentes están en `pruebas.md`. Desde la versión 3, `python respaldo.py verificar` imprime un aviso, no un error, si el respaldo es anterior al UUID de dispositivos. Se puede restaurar y la aplicación lo migra al abrirlo.

El 18 de noviembre de 2026, con Python 3.13 y SQLite 3.53, se ejecutaron 42 pruebas con `unittest`. Todas pasaron: 6 originales del equipo, 18 de SCRUM-20 y 18 de SCRUM-21. En PostgreSQL 16 local se ejecutaron los escenarios 003 y 004, la verificación de retención con 9 de 9 comprobaciones en verdadero, y la simulación de respaldo con `pg_dump` y `pg_restore`, con ocho verificaciones en verdadero.

**Realizado con simulación.** La restauración de Supabase se simuló con PostgreSQL local. No se probó ninguna función de respaldo de Supabase.

**Pendiente en infraestructura real.**
- Ejecutar 003 y 004 en el proyecto real de Supabase y correr `verificar_retencion.sql`.
- Confirmar el plan de Supabase y si hay respaldos diarios.
- Probar `pg_dump` contra la cadena de conexión real. La conexión directa puede requerir IPv6 o el pooler.
- Restaurar un respaldo real de Supabase en un proyecto nuevo.
- Verificar el comportamiento en Windows. Lo ejecutado fue en Linux.

## Errores encontrados y correcciones

Una prueba mía fallaba porque el trigger de auditoría de lectura también registraba el evento dañado a propósito. Se corrigió la prueba para quitar ese trigger. Un guion de respaldo daba un falso fallo por usar `pipefail` con una salida de error esperada. Se corrigió capturando la salida.
