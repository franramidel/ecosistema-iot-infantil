# Nido · Usuarios y cuentas de la cuna inteligente

Módulo de Persona 1, historias SCRUM-15 a SCRUM-19, 19 puntos. Incluye panel web adaptable en español, servidor Python, esquema de Supabase, simulador y pruebas. El mismo panel funciona con ambos modos de almacenamiento.

## Ejecutar la demo local

Requiere Python 3.10 o posterior con SQLite y scrypt (incluidos en Python de python.org). Extrae el ZIP y abre una terminal en la carpeta que contiene `app.py`.

```powershell
python app.py
```

En Windows también puedes usar `py -3 app.py`. En el primer arranque escribe tu correo y elige una contraseña de al menos 10 caracteres. Al escribirla en la terminal el cursor permanece quieto. Abre **http://127.0.0.1:8000** e inicia sesión. Se guarda el trabajo en `datos/demo.sqlite3`. Detén el servidor con Ctrl+C. Reiniciar conserva los perfiles y las lecturas, y requiere iniciar sesión de nuevo. La demo funciona sin instalar paquetes y sin configurar servicios externos.

## Demostración para el profesor

| Historia | Acciones en la demo | Resultado observable |
| --- | --- | --- |
| SCRUM-15 · Autenticación | Entra con tu correo. Desde una ventana privada intenta entrar con una clave errónea y luego con un correo inexistente. | La sesión válida abre el panel. Ambos fallos muestran «Correo o contraseña incorrectos». En SQLite, la columna `password_hash` contiene una sal aleatoria y un hash scrypt. En Supabase el hash lo gestiona Auth. |
| SCRUM-18 · Multibebé | Crea los perfiles Luna y Sol desde **Nuevo perfil**. En **Dispositivos**, registra `TEMP-01`, copia su clave y asígnalo a Luna. Envía una lectura con el comando de abajo. | Al seleccionar a Luna aparecen el sensor, el gráfico y el historial. Al seleccionar a Sol, su ficha está vacía. **Editar nombre** modifica el perfil. |
| SCRUM-16 · Roles | En **Personas**, crea una cuenta de cuidador con permiso de solo lectura. Entra con esa cuenta en una ventana privada. Después habilita **Permitir operar dispositivos** desde la cuenta administradora. | El cuidador ve el estado y recibe el permiso en la sesión abierta. Puede activar el dispositivo cuando se le concede. La API comprueba los permisos para crear usuarios, reasignar y exportar. |
| SCRUM-17 · Multicuenta | Desde **Personas**, crea una segunda cuenta con rol **Padre / administrador**. Ábrela en otro perfil del navegador y selecciona a Luna en ambas ventanas. Activa o desactiva el sensor en una de ellas. | Ambas cuentas ven el mismo cambio sin recargar. Dos pestañas del mismo perfil comparten la cookie y representan una sola sesión. |
| SCRUM-19 · Reasignación | Con una lectura de 22.4 en Luna, elige **Dispositivos → Reasignar → Sol**. Lee la advertencia y confirma. Envía otra lectura de 24.1. Revisa las dos fichas y sus CSV. | Luna conserva 22.4. Sol recibe 24.1 y muestra el hardware activo. El selector también permite dejar el dispositivo sin asignar. |

Ejecuta el simulador en una **segunda terminal**, desde la misma carpeta. Pega la clave privada de `TEMP-01` cuando la pida.

```powershell
python simular.py --device TEMP-01 --valor 22.4
```

Después del traslado repite con `--valor 24.1`. Para simular humedad añade `--medida Humedad --unidad "%"`. La línea JSON que imprime el simulador contiene la hora y el UUID de la lectura.

## Conectar el proyecto de Supabase

1. En un proyecto de pruebas vacío habilita la Data API sobre el esquema `public` y el proveedor de correo/contraseña. Desactiva el registro público de usuarios en la configuración de Auth para que las altas pasen por el administrador.
2. En el editor SQL ejecuta `supabase/001_usuarios.sql` una vez. La migración crea las tablas, las políticas RLS, las funciones de escritura y el historial.
3. En **Authentication → Users**, crea y confirma la primera cuenta con correo y contraseña. En `supabase/002_primer_administrador.sql`, sustituye `CAMBIAR@example.test` por ese correo y ejecuta el script. Crea la familia inicial y su membresía administradora. Las siguientes cuentas de esa familia se crean desde el panel.
4. Copia `.env.example` como `.env` junto a `app.py`. Completa `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY` y `SUPABASE_SECRET_KEY` con los valores del proyecto. Guarda la clave secreta sólo en el servidor. La Raspberry Pi usa su propia clave de dispositivo.
5. En **Database → Publications**, confirma que la tabla `public.families` está incluida en la publicación `supabase_realtime` y que Realtime está habilitado para el proyecto. La migración la añade a la publicación cuando los permisos de la base lo permiten. Ejecuta `python app.py --supabase`, abre la misma dirección e inicia sesión con la primera cuenta de Supabase. Repite el recorrido de la demo. La demo SQLite y Supabase tienen datos independientes.

El servidor usa la clave pública y el JWT del usuario para las lecturas y mutaciones de la familia. Comprueba el rol antes de usar la API administrativa de Auth para crear un usuario. Un trigger de PostgreSQL crea la membresía en la misma transacción que la identidad. La interfaz recibe una cookie de sesión HttpOnly con duración de ocho horas. Sirve el prototipo tras HTTPS y establece `CUNA_SECURE_COOKIE=1` si lo publicas. El servidor incluido está pensado para la demostración del curso.

Documentación oficial de referencia: [seguridad de contraseñas](https://supabase.com/docs/guides/auth/password-security), [claves de API](https://supabase.com/docs/guides/getting-started/api-keys) y [Postgres Changes](https://supabase.com/docs/guides/realtime/postgres-changes).

## Contrato para el equipo

**Acceso familiar.** Una familia agrupa varias cuentas. Los padres administradores y cuidadores vinculados ven los perfiles de esa familia. El permiso de operación del cuidador se aplica a sus dispositivos familiares. Para vincular a la pareja, un administrador crea otra identidad con rol administrador en su misma familia. Cada identidad de esta versión pertenece a una familia. La separación por familia se verifica en el servidor y, con Supabase, en la base mediante RLS.

**Perfiles y dispositivos.** `children.id` corresponde a Child_ID y `devices.id` a Device_ID. `assignments` registra intervalos de asignación con inicio incluido y fin excluido. La base admite una sola fila abierta por dispositivo. El traslado cierra y abre los intervalos en una transacción. Cada orden de operación o traslado incluye el identificador de la asignación que vio el usuario, de modo que una pantalla desactualizada recibe un conflicto si el sensor ya se movió. Los botones Activar/Desactivar modifican el estado lógico de la asignación para que otros módulos lo consuman. El nuevo destino comienza inactivo.

**Ingreso de telemetría.** Desde Python se puede importar `enviar_lectura(url, clave, evento)` de `simular.py`. El punto de entrada es `POST /api/events` con `Content-Type: application/json` y `Authorization: Bearer <clave del dispositivo>`. Al registrar un dispositivo aparece una clave aleatoria de uso exclusivo para ese Device_ID. Ejemplo de cuerpo:

```json
{
  "event_id": "9a2bdf39-fd76-4f3f-9c98-bdb85f198608",
  "device_id": "TEMP-01",
  "captured_at": "2026-11-13T14:22:00-06:00",
  "metric": "Temperatura",
  "value": 22.4,
  "unit": "°C"
}
```

Usa la hora real de captura con zona horaria y un UUID nuevo por muestra. El servidor devuelve `{"child_id":"…"}` y conserva ese bebé en el evento. Las capturas tardías se resuelven con el intervalo histórico. Reintentar con el mismo UUID y contenido devuelve el mismo resultado. Un UUID reutilizado con otros datos o una captura durante un intervalo sin asignación devuelve 409. La Pi debe mantener su reloj sincronizado. Para probar desde otra máquina de la red de laboratorio, ejecuta el servidor con `--host 0.0.0.0` y pasa `--url http://IP-DE-LA-COMPUTADORA:8000` al simulador.

**Consulta y sincronización.** Con la cookie de sesión, `GET /api/state?child=<UUID>` devuelve miembros visibles, inventario familiar, perfiles y las últimas 100 lecturas del bebé solicitado. La ficha filtra el inventario por `child_id`. El gráfico agrupa por medida y unidad. `GET /api/report?child=<UUID>` genera el CSV del historial completo para administradores. En modo local, `GET /api/stream` usa Server-Sent Events y un sondeo de versión cada tres segundos para detectar escrituras de otros procesos. En modo Supabase, el panel usa Postgres Changes sobre `public.families`, filtrado al `family_id` de la sesión y validado por RLS; las notificaciones sólo invalidan el estado y cada cliente vuelve a consultar `/api/state` con su sesión autorizada. Al suscribirse o reconectarse, el panel vuelve a consultar el estado para recuperar cambios perdidos. El navegador recibe únicamente la clave publicable y el JWT de la sesión, nunca la clave secreta.

**Dónde extender.** `app.py` define HTTP y sesiones. `cuna/common.py` valida entradas. `cuna/cloud.py` conecta a Auth y PostgREST. `cuna/local.py` implementa la demo. `web/` contiene el panel. Para integrar un sensor, empieza por la función de envío y conserva el identificador de la muestra hasta recibir confirmación. La actuación física, los umbrales y Telegram pertenecen a los módulos de hardware y alertas del equipo.

## Repetir las pruebas

```powershell
python -m unittest discover -s tests -v
```

Las pruebas arrancan un servidor HTTP local y una base temporal por caso. Cubren los errores de acceso, la sesión cerrada, los permisos revocados, dos sesiones sincronizadas, el aislamiento familiar, las lecturas tardías y duplicadas, el instante exacto del traslado y dos reasignaciones simultáneas. Para validar el entorno Supabase del equipo, completa su configuración y repite el recorrido anterior con dos cuentas.


## Dónde se cumple cada criterio de aceptación

| Historia | Criterio | Dónde está |
| --- | --- | --- |
| SCRUM-15 Autenticación | CA1 acceso con credenciales válidas | `POST /api/login` en `app.py`; `Local.login` o `Cloud.login` |
| | CA2 contraseña con hash | `password_hash` (scrypt con sal) en `cuna/local.py`; Supabase Auth en modo nube |
| | CA3 error sin revelar el campo | Mismo mensaje «Correo o contraseña incorrectos» en ambos adaptadores |
| SCRUM-16 Roles | CA1 roles padre y cuidador | Columna `role` de `members` |
| | CA2 solo el administrador crea usuarios, reasigna y exporta | `Local.change` y `export_page`; en nube, `cuna_change` y `Cloud.export_page` |
| | CA3 el cuidador opera según permisos | `can_operate` y la acción `operate` |
| SCRUM-17 Multicuenta | CA1 varias cuentas ven los mismos bebés y dispositivos | `family_id` compartido por las cuentas de la familia |
| | CA2 cambios en tiempo real | SSE local en `GET /api/stream`; Supabase Postgres Changes desde `web/app.js`, con configuración de sesión en `GET /api/sync` |
| SCRUM-18 Multibebé | CA1 varios perfiles | Acción `child` |
| | CA2 datos independientes | Cada evento guarda su `child_id` |
| | CA3 filtros y gráficos del bebé elegido | `state(selected)` y `draw()` en `web/app.js` |
| SCRUM-19 Reasignación | CA1 lista por Device_ID y traslado | Pestaña Dispositivos, acción `assign` |
| | CA2 aviso de archivado antes de confirmar | Texto del diálogo en `web/app.js` |
| | CA3 y CA4 eventos anteriores y posteriores | Intervalos en `assignments` y evento ligado a su `child_id` |
| | CA5 un solo bebé activo por dispositivo | Índice único `one_active` |
| | Eventos inmutables | Triggers `keep_event` y `keep_history` (SQLite) y `immutable_event` (Supabase) |

Pruebas automáticas: `python -m unittest discover -s tests -v` (incluye sincronización local, configuración Realtime con cuentas de una misma familia y aislamiento entre familias; la entrega real de Supabase requiere verificarla con un proyecto configurado).

**Alertas por Telegram (SCRUM-24).** Se ejecutan con un proceso aparte del servidor: `python telegram_worker.py`. Necesita `TELEGRAM_BOT_TOKEN` en el entorno o en `.env`, que no se sube a Git. Con Supabase se inicia con `python telegram_worker.py --supabase`. Guía en `docs/integracion-windows.md`, sección 8.
