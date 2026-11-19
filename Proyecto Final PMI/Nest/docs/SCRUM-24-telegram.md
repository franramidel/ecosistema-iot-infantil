# SCRUM-24 · RF-NOT-01 Alertas autónomas por Telegram

Este documento describe el servicio que recibe una solicitud de alerta y la entrega por Telegram. No decide cuándo hay una alerta. Eso es SCRUM-25.

## Qué cumple

| Criterio | Cómo se cumple | Cómo se verifica |
| --- | --- | --- |
| CA1. Un bot envía notificaciones a la cuenta vinculada | El procesador toma la cola y llama a `sendMessage` del bot hacia el `chat_id` verificado | Pruebas 06, 17 y 39 contra un Telegram simulado |
| CA2. El envío no depende de que la web o la app estén abiertas | El envío ocurre en un proceso aparte, `telegram_worker.py`. La solicitud queda guardada en SQLite antes de responder | Prueba 32 apaga el servidor web y la cola se envía igual. La prueba 47 lanza `telegram_worker.py` como proceso real. Prueba 28 reinicia el servidor y la solicitud sigue ahí |

Estado real: todo se verificó con simulaciones y con PostgreSQL local, con SQLite y con Supabase simulado. Nunca se envió un mensaje a Telegram real ni se conectó a un proyecto real de Supabase. Ver Limitaciones.

## Arquitectura

```text
Dispositivo IoT -> Backend -> detección de eventos (SCRUM-25) -> solicitar_notificacion()
                                                                        |
                                                                        v
                                                         notification_queue (SQLite)
                                                                        |
                                              telegram_worker.py (proceso aparte)
                                                                        |
                                                              API de Telegram -> usuario
```

No hay RabbitMQ, Redis ni microservicios. La cola es una tabla.

### Por qué un proceso aparte

El servidor web `http.server` atiende solicitudes. Si el envío viviera en un hilo del servidor o en un temporizador de JavaScript, cerrar el navegador o reiniciar el servidor perdería alertas. Con un proceso aparte pasa lo contrario: la solicitud ya está en disco cuando `solicitar_notificacion` regresa, y el procesador la toma cuando puede.

En Windows se inicia con `python telegram_worker.py` en su propia terminal. Es un proceso común, sin servicios de Windows ni dependencias. Para dejarlo siempre activo se puede registrar en el Programador de tareas de Windows con el disparador al iniciar sesión. Eso queda fuera del código y se documenta como siguiente paso.

### Por qué long polling y no webhook

Un webhook exige una URL pública con HTTPS. La demo corre en `localhost` de Windows y no tiene dominio. Con long polling el procesador pregunta a Telegram con `getUpdates` y no necesita puertos abiertos. Un solo procesador puede consultar las actualizaciones a la vez, porque Telegram devuelve error 409 si dos consultan el mismo bot. Eso lo garantiza un arrendamiento en la tabla `telegram_worker`.

Para un entorno desplegado se puede cambiar a webhook: se registra la URL con `setWebhook` y una ruta nueva en `app.py` entrega cada actualización al mismo código de vinculación que hoy usa `procesar_actualizaciones`. La cola y el envío no cambian.

## Archivos

| Archivo | Responsabilidad |
| --- | --- |
| `cuna/telegram.py` | Cliente HTTP mínimo. Solo biblioteca estándar. Clasifica errores y borra el token de cualquier mensaje |
| `cuna/notificaciones.py` | Esquema SQLite, códigos de vinculación, cola, reclamo atómico, resultados, arrendamiento y mensaje |
| `cuna/procesador.py` | Ciclo del procesador: cola y actualizaciones del bot |
| `telegram_worker.py` | Programa que se ejecuta. Lee `.env`, elige SQLite o Supabase con `--supabase`, valida el bot con `getMe` y arranca el procesador |
| `solicitar_alerta.py` | Encola una alerta de prueba sin navegador, para comprobar CA2 |
| `cuna/local.py` | Métodos de `Local` para la interfaz y el contrato |
| `app.py` | Cuatro rutas HTTP, iguales con los dos almacenes |
| `cuna/cloud.py` | Los mismos métodos de Telegram sobre Supabase, por RPC |
| `supabase/007_telegram.sql` | Tablas, RLS y funciones para Supabase |
| `web/` | Tarjeta Alertas por Telegram |

## Modelo de datos

| Tabla | Propósito | Notas |
| --- | --- | --- |
| `telegram_links` | Una fila por usuario con su `chat_id` | `chat_id` es único. Desvincular lo pone en `NULL`, no borra la fila |
| `telegram_codes` | Códigos temporales | Se guarda el hash SHA-256, nunca el código. Un índice único parcial permite un solo código abierto por usuario |
| `telegram_attempts` | Intentos fallidos por chat | Cinco fallos en 15 minutos bloquean al chat |
| `telegram_state` | Offset de `getUpdates` | Se guarda en la misma transacción que la vinculación |
| `telegram_worker` | Arrendamiento del procesador | Una sola fila |
| `notification_queue` | Solicitudes | Estado, intentos, siguiente intento, último error, evento relacionado, destinatario |

La cola tiene `unique(family_id, idempotency_key)`, claves foráneas a `members`, `families` y `audit_log`, restricciones de largo y de estado, e índices por estado y fecha. Nada se borra: la retención de SCRUM-21 no se toca y la prueba de escaneo de código sigue prohibiendo `delete from`.

### Estados de una solicitud

```text
pendiente -> procesando -> enviada
procesando -> error_recuperable -> procesando   (hasta 5 intentos)
procesando -> error_permanente                  (error definitivo o intentos agotados)
sin_vinculo                                     (el usuario no tenía Telegram al pedirla)
```

## Flujo de vinculación

1. El usuario inicia sesión y pulsa Vincular Telegram. Esto llama `POST /api/telegram/link`.
2. El servidor genera un código de 8 caracteres con `secrets.choice`. El alfabeto no tiene 0, O, 1, I ni L. Vence en 10 minutos. Un código nuevo anula el anterior.
3. La página muestra el código y un enlace `t.me/<bot>?start=<código>`.
4. El usuario abre el bot y lo inicia. Telegram entrega el mensaje al procesador por `getUpdates`.
5. El procesador valida que el chat sea privado, que el remitente no sea un bot y que `chat.id` sea igual a `from.id`. Luego llama `telegram_vincular`.
6. Si el código es válido, no está usado ni vencido, y el chat no pertenece a otra cuenta, se guarda el `chat_id` y se marca el código como usado.
7. El bot responde con la confirmación. La página muestra Telegram vinculado al actualizar.

El `chat_id` nunca viene del navegador. Sale del mensaje real que Telegram entregó al bot. Eso prueba que la persona controla ese Telegram. El usuario debe iniciar la conversación con el bot: Telegram no permite que un bot escriba primero.

Errores: un código incorrecto o repetido responde que no es válido. Un código vencido lo dice. Cinco fallos desde un chat lo bloquean 15 minutos, incluso si después manda un código bueno. Un chat ya vinculado a otra cuenta no puede reclamarse y el código no se consume.

Desvincular pone `chat_id` en `NULL`, revoca los códigos abiertos y deja las solicitudes nuevas en `sin_vinculo`.

## Flujo de una notificación

1. Código del servidor llama `solicitar_notificacion`.
2. Se compone el mensaje y se guarda una fila `pendiente`. La respuesta sale después del `commit`.
3. El procesador reclama filas listas dentro de una transacción `begin immediate`. Marca `procesando`, suma un intento y fija un arrendamiento de 2 minutos con un token propio.
4. Resuelve el `chat_id` del destinatario en ese momento. Si ya no existe, el error es permanente.
5. Llama `sendMessage` con tiempo límite de 15 segundos.
6. Cierra el intento con el token. Si el token ya no coincide, el resultado se ignora.

### Reintentos

| Resultado de Telegram | Clasificación | Qué pasa |
| --- | --- | --- |
| Red caída, tiempo agotado, respuesta no JSON, 5xx | temporal | Se reintenta con espera de 30 s, 2 min, 8 min y 30 min |
| 429 | temporal | Se respeta `retry_after`, hasta 1 hora |
| 401 | token | Se detiene el envío, se informa el error de configuración y la cola se conserva |
| 400, 403, 404 | permanente | No se reintenta. El 403 marca el vínculo como bloqueado |
| Intentos agotados, 5 en total | permanente | Queda `error_permanente` con el último error |

Cada intento incrementa el contador al reclamar, no al terminar. Si el proceso muere en cada intento, el límite se alcanza igual y no hay bucle infinito.

### Duplicados e idempotencia

La clave de idempotencia es única por familia. Repetir la misma clave con el mismo contenido devuelve la solicitud existente con `duplicado: true`. Repetirla con otro contenido da error 409.

No se promete entrega exactamente una vez. Si el procesador muere después de que Telegram aceptó el mensaje y antes de guardar `enviada`, el arrendamiento vence y el mensaje se envía otra vez. Es entrega al menos una vez. Se eligió porque en una alerta de un bebé es peor perder el aviso que repetirlo.

## Contrato para SCRUM-25

Francisco no necesita conocer la cola ni Telegram. Llama un método:

```python
resultado = store.solicitar_notificacion(
    usuario_id,              # str. id del miembro destinatario
    clave_idempotencia,      # str, 1 a 128 caracteres. Por ejemplo "temp-alta:" + id_evento
    *,
    titulo,                  # str, 1 a 150. Texto de la alerta
    dispositivo=None,        # str, hasta 64. Device_ID visible
    ocurrido=None,           # str ISO 8601 o None. Se muestra en hora de Costa Rica
    estado="Advertencia",    # "Informativo", "Advertencia" o "Crítico"
    evento_id=None,          # str. id de audit_log. Debe ser de la misma familia
)
```

Devuelve `{"id", "estado", "duplicado", "vinculado"}`.

Excepciones: `Problem` con estado 404 si el usuario o el evento no existen en la familia, 409 si la clave ya se usó con otro contenido, y 400 si un campo no es válido.

Sin Telegram vinculado: no falla. Guarda la solicitud como `sin_vinculo` y devuelve `vinculado: False`. Francisco decide si avisa por otro medio.

Si Telegram falla: `solicitar_notificacion` ya regresó. El procesador reintenta y deja el resultado en la cola y en la auditoría.

Autorización: es una función interna de Python, sin ruta HTTP. La llama código del servidor que ya validó el evento. Ningún dispositivo ni navegador puede invocarla. La prueba 36 usa un evento de ejemplo.

```python
evento = ...  # fila de audit_log creada por /api/audit/events
store.solicitar_notificacion(miembro, "temp-alta:" + evento["id"],
    titulo="Temperatura fuera de rango", dispositivo="CUNA-1",
    ocurrido=evento["occurred_at"], estado="Crítico", evento_id=evento["id"])
```

## Integración con la auditoría de SCRUM-20

Cuando se pasa `evento_id`, al cerrar la solicitud se agrega una fila a `audit_log` con tipo `NOTIFICACION_ENVIADA` o `NOTIFICACION_FALLIDA`, el bebé y el dispositivo del evento original, y un `source_event_id` propio. No toda fila de auditoría genera un mensaje. Solo los genera quien llama `solicitar_notificacion`.

Ambos tipos están en `auditoria.RESERVADOS`: un dispositivo que intente enviarlos recibe 403. En Supabase lo impide un trigger que exige una variable de sesión que solo pone la función de cierre.

## Formato del mensaje

```text
Ecosistema IoT Infantil

Alerta: Temperatura fuera de rango
Dispositivo: CUNA-1
Fecha: 18/11/2026
Hora: 14:00
Estado: Crítico
```

Los datos salen del evento. Todo valor variable pasa por `html.escape`, y se quitan los caracteres de control. Se usa `parse_mode=HTML`, porque exige escapar solo tres caracteres. El mensaje no incluye el nombre del bebé.

## Endpoints

| Ruta | Método | Efecto |
| --- | --- | --- |
| `/api/telegram` | GET | Estado propio: vinculado, bloqueado, código vigente, procesador activo, última prueba, nombre del bot |
| `/api/telegram/link` | POST | Genera código para el usuario de la sesión. Devuelve código, vencimiento y enlace |
| `/api/telegram/unlink` | POST | Desvincula al usuario de la sesión |
| `/api/telegram/test` | POST | Encola una notificación de prueba propia. Una cada 15 segundos |

Todas exigen sesión. Cada usuario solo opera sobre sí mismo, porque el id sale de la sesión y no del cuerpo. Los POST conservan las defensas existentes: JSON obligatorio, Origin igual a Host y cookie `SameSite=Strict`. Funcionan con SQLite y con Supabase. Solo responden 501 si el almacén no implementa Telegram.

## Configuración

| Variable | Uso | Secreta |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | Token del bot, lo entrega BotFather | Sí |
| `TELEGRAM_BOT_USERNAME` | Nombre del bot sin arroba, para el enlace | No |
| `TELEGRAM_API_BASE` | Solo pruebas con servidor falso. Por defecto `https://api.telegram.org` | No |
| `CUNA_DB` | Ruta de SQLite, compartida con el servidor | No |

`.env.example` trae las variables vacías. `.env` está en `.gitignore`. El servidor web nunca lee el token: solo lo lee `telegram_worker.py`. Un cliente solo acepta HTTPS, salvo `localhost` para pruebas.

## Seguridad

- El token vive solo en el entorno del procesador. Se borra de cualquier error antes de guardarlo o imprimirlo. Una prueba revisa que ningún archivo del repositorio contenga algo con forma de token.
- El código de vinculación se guarda como hash, es aleatorio, de un solo uso y vence.
- Los intentos fallidos se limitan por chat.
- El chat se verifica por interacción real con el bot, no por lo que diga el navegador.
- Un usuario no vincula ni consulta a otro. Las claves de idempotencia son por familia. Un evento de otra familia se rechaza.
- Los registros del procesador no incluyen el contenido de los mensajes ni el token.
- No se alteró ninguna política de seguridad existente. Los endpoints nuevos usan las mismas defensas.

## Persistencia y recuperación

| Situación | Resultado |
| --- | --- |
| Se cierra el navegador | Nada cambia. El envío no depende de él |
| Se reinicia el servidor web | La solicitud sigue guardada, en SQLite o en Supabase |
| Muere el procesador a mitad de un envío | El arrendamiento vence y otro procesador la recupera |
| Telegram está caído | La solicitud queda `error_recuperable` y se reintenta, hasta 5 veces |
| El token es inválido | Se detiene el envío, se informa y la cola se conserva |
| Se vuelve a iniciar con token corregido | La cola se vacía sola |

## Pruebas

51 pruebas en `tests/test_telegram.py` y 56 en `tests/test_telegram_nube.py`, más `tests/postgres/escenario_007.sql` y `tests/ui/verificar_telegram.js`. Las de Python usan un servidor HTTP falso que imita `api.telegram.org` y registra lo que recibe. Ninguna sale a Internet.

| Tipo | Qué cubre |
| --- | --- |
| Simulación | Cliente contra el servidor falso: envío, 401, 403, 400, 429, 503, tiempo agotado, red caída |
| Integración local | SQLite real, servidor web real, procesador real, migración de la versión 3 a la 4 |
| PostgreSQL local | `007_telegram.sql` aplicado dos veces y su escenario, con 30 comprobaciones |
| Telegram real | No se hizo. Ver la guía manual |

Las pruebas están numeradas del 01 al 51. Incluyen el proceso real del procesador, con token presente, ausente y rechazado. Cubren vinculación válida, código vencido, repetido y erróneo, bloqueo por intentos, sin sesión, cuenta ajena, desvincular, envío exitoso, red, tiempo agotado, token inválido, usuario sin Telegram, error recuperable y permanente, reintentos limitados, duplicados, persistencia, reinicio del procesador, concurrencia, aislamiento entre familias, seguridad de endpoints, auditoría, contrato de SCRUM-25, operación sin navegador, SQLite, PostgreSQL local y ausencia de credenciales.

### Prueba manual con un Telegram real

Solo el usuario puede hacerla, porque requiere su bot.

1. Crea el bot con BotFather y copia el token.
2. Define `TELEGRAM_BOT_TOKEN` y `TELEGRAM_BOT_USERNAME` en `.env`.
3. Inicia el servidor y, en otra terminal, `python telegram_worker.py`.
4. En la página pulsa Vincular Telegram, abre el enlace y pulsa Iniciar en Telegram.
5. Pulsa Enviar prueba. Debe llegar el mensaje en pocos segundos, con la página abierta o cerrada.
6. Cierra el navegador, pide otra prueba desde otra sesión y confirma que llega.

## Supabase

Los dos almacenes comparten un contrato. `Local` usa SQLite y `Cloud` usa Supabase por RPC. Los nombres, los parámetros y las respuestas son los mismos, y una prueba ejecuta cada caso contra los dos y compara el resultado.

| Método | Quién lo usa | SQLite | Supabase, función RPC y rol |
| --- | --- | --- | --- |
| `telegram_estado(sesion)` | Interfaz web | `notificaciones.estado` | `cuna_telegram_status`, rol authenticated |
| `telegram_codigo(sesion)` | Interfaz web | `notificaciones.generar_codigo` | `cuna_telegram_start`, authenticated |
| `telegram_desvincular(sesion)` | Interfaz web | `notificaciones.desvincular` | `cuna_telegram_stop`, authenticated |
| `telegram_probar(sesion)` | Interfaz web | cola con clave `prueba:` | `cuna_telegram_test`, authenticated |
| `solicitar_notificacion(...)` | SCRUM-25, código del servidor | `notificaciones.encolar` | `cuna_notify`, service_role |
| `telegram_vincular(texto, chat, usuario, offset)` | Procesador | `notificaciones.vincular` | `cuna_telegram_link`, service_role |
| `cola_reclamar(token, limite)` | Procesador | `notificaciones.reclamar` | `cuna_queue_claim`, service_role |
| `cola_resultado(trabajo, ...)` | Procesador | `notificaciones.resultado` | `cuna_queue_result`, service_role |
| `telegram_procesador(dueno)` | Procesador | arrendamiento en SQLite | `cuna_telegram_lease`, service_role |
| `telegram_offset(nuevo)` | Procesador | tabla `telegram_state` | `cuna_telegram_offset`, service_role |

Las acciones del usuario usan su propio token de sesión y funciones que leen `auth.uid()`. El servidor web no usa la clave de servicio para ninguna de ellas. La clave de servicio solo se usa en `solicitar_notificacion` y en el procesador, que corren en el servidor o en `telegram_worker.py`. Nunca llega al navegador: la interfaz solo recibe estado, código y enlace.

`007_telegram.sql` deja las seis tablas con RLS activa y sin políticas de escritura. Un usuario autenticado lee su propio vínculo sin ver el `chat_id`. Las funciones del procesador exigen `service_role`: un usuario autenticado o anónimo que las llame recibe 403. El reclamo usa `for update skip locked`, de modo que dos procesadores no toman la misma solicitud.

Diferencias de comportamiento entre los dos almacenes:

| Aspecto | SQLite | Supabase |
| --- | --- | --- |
| Reloj | El del proceso, inyectable en pruebas | El de la base de datos. El parámetro `ahora` se ignora |
| Vincular | Devuelve `(estado, member_id)` | Devuelve `(estado, None)`. El procesador no usa el segundo valor |
| Actualización de familia | Sube `families.version` al vincular y desvincular | Igual, dentro de las funciones |
| Eventos de notificación falsos | `auditoria.RESERVADOS` los rechaza con 403 | Un trigger exige una variable de sesión que solo pone `cuna_queue_result`. También da 403 |

### Cómo elegir el almacén

El servidor y el procesador deben usar el mismo.

```powershell
python app.py                          # SQLite
python telegram_worker.py              # SQLite
python app.py --supabase               # Supabase
python telegram_worker.py --supabase   # Supabase
```

Con `--supabase` el procesador necesita `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY` y `SUPABASE_SECRET_KEY`. Si falta una, termina con un mensaje claro y código 2. Si Supabase rechaza la clave de servicio, termina con código 5. Si Supabase o Telegram no responden, el procesador no se cae: lo registra y vuelve a intentar cada 5 segundos.

### Qué se probó y qué no

Se probó el SQL real de 001 a 007 en PostgreSQL 16 local, con los roles `authenticated`, `anon` y `service_role` de PostgreSQL. Se llegó a él desde `cuna/cloud.py` por un puente HTTP de pruebas, `tests/puente_postgrest.py`, que ejecuta cada RPC con el rol que corresponde a la credencial.

El puente no es Supabase. No hay PostgREST, Supabase Auth, JWT firmados ni red real. Por eso queda sin verificar que un proyecto real acepte la clave `sb_secret_` o la clave `service_role` heredada como rol de servicio para estas funciones, que PostgREST exponga las funciones con los parámetros nombrados que se usan, y que los permisos y la RLS del proyecto coincidan con los de PostgreSQL local. La guía de Windows incluye los pasos para comprobarlo.

## Limitaciones

1. Nunca se probó contra Telegram real ni contra un proyecto de Supabase real. Todo es simulación y PostgreSQL 16 local, con un puente HTTP de pruebas que no es PostgREST. SCRUM-24 no debe darse por totalmente validada hasta correr la guía de pruebas reales.
2. Con Supabase, `cuna/cloud.py` llama las funciones RPC y no hay respuestas 501. Esa ruta solo se ejercitó con el puente de pruebas.
3. Entrega al menos una vez. Puede haber un mensaje repetido tras una interrupción.
4. El procesador debe estar corriendo. Si no está, las solicitudes esperan y la página lo advierte. No se instala como servicio de Windows.
5. Si el usuario bloquea al bot, el envío falla de forma permanente. Debe desbloquearlo y volver a vincular.
6. Sin un segundo canal, una alerta crítica a un usuario sin Telegram queda en `sin_vinculo`. SCRUM-25 decide cómo avisar.
7. El arrendamiento del procesador asume que los relojes del equipo no saltan. Con Supabase usa el reloj de la base.
8. El mensaje usa la hora de Costa Rica fija en UTC-6, sin horario de verano, que el país no usa.
9. No se integró con los módulos del equipo. La interfaz con SCRUM-25 es un contrato probado con un evento de ejemplo.
10. La clave de servicio de Supabase está en el equipo que corre el servidor y el procesador. Quien tenga acceso a ese equipo la tiene. En producción debe guardarse en un gestor de secretos.
