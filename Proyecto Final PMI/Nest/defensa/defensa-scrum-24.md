# Defensa de SCRUM-24 y de las cuatro historias

Guía para explicar el trabajo y responder preguntas. Las respuestas dicen solo lo que se verificó.

## Cómo funciona un bot de Telegram

Un bot es una cuenta de Telegram que controla un programa. BotFather lo crea y entrega un token. El token es la llave del bot: quien lo tiene puede enviar mensajes como el bot y leer lo que le escriben. Por eso es un secreto.

El bot no tiene un servidor propio dentro de Telegram. Es una API HTTPS. Todo se hace con llamadas a `https://api.telegram.org/bot<token>/<método>`.

## Qué parte de la API se usa

| Método | Para qué |
| --- | --- |
| `getMe` | Comprobar al iniciar que el token es válido |
| `getUpdates` | Recibir los mensajes que los usuarios escriben al bot, con long polling |
| `sendMessage` | Enviar una alerta o una respuesta del bot |

Un bot no puede escribirle a alguien que no lo haya iniciado. Por eso el usuario debe pulsar Iniciar antes de recibir alertas.

## Cómo envía el backend

El procesador toma una solicitud de la cola, busca el `chat_id` del destinatario y hace un POST a `sendMessage` con el texto. Telegram responde con éxito o con un código de error. El cliente clasifica el error y la cola decide si reintenta.

## Cómo funciona la vinculación

La web genera un código aleatorio de 8 caracteres que vence en 10 minutos. El usuario se lo manda al bot desde su Telegram. Telegram entrega ese mensaje al procesador con el `chat_id` verdadero de quien lo escribió. El sistema une ese `chat_id` con la cuenta que generó el código.

La seguridad está en que el `chat_id` no lo dice el navegador. Lo dice Telegram. Si alguien adivinara un código ajeno, necesitaría hacerlo en 10 minutos, con 32 elevado a 8 combinaciones y solo cinco intentos por chat cada 15 minutos.

## Por qué el token es secreto

Con el token cualquiera puede hacerse pasar por el bot. Por eso no está en el código ni en GitHub. Entra por una variable de entorno o por `.env`, que está ignorado por Git. Solo lo lee el procesador. Los errores lo reemplazan por `***` antes de registrarse. Si se filtra, se revoca en BotFather y se genera otro.

## Por qué una cola persistente

Una alerta no debe perderse porque Telegram esté caído un minuto o porque el servidor se haya reiniciado. La cola es una tabla en la base. La solicitud se guarda antes de contestar. Si algo falla, la fila sigue ahí y se reintenta.

## Reintentos

Los errores temporales se reintentan con espera creciente: 30 segundos, 2 minutos, 8 minutos y 30 minutos, hasta 5 intentos. Los errores definitivos, como un chat inexistente, no se reintentan. Así no se insiste sin fin y tampoco se abandona un fallo pasajero.

## Idempotencia

Cada solicitud lleva una clave elegida por quien la pide, por ejemplo el id del evento. Si la misma clave llega dos veces, se devuelve la solicitud original. Así un reintento de SCRUM-25 no manda dos mensajes. Es única por familia y exige el mismo contenido.

## Cómo no se pierden solicitudes

1. Se guardan antes de responder.
2. Sobreviven al reinicio, porque están en SQLite.
3. Al reclamarlas tienen un arrendamiento de 2 minutos. Si el procesador muere, el arrendamiento vence y otro la toma.
4. Los intentos cuentan al reclamar, así que una solicitud que mata al procesador no lo hace girar para siempre.

## Cómo interactúan las cuatro historias

| Historia | Aporte | Relación con SCRUM-24 |
| --- | --- | --- |
| SCRUM-20 Auditoría | Registro unificado e inmutable, con el UUID del dispositivo | Cada alerta puede enlazarse a un evento y deja `NOTIFICACION_ENVIADA` o `NOTIFICACION_FALLIDA` |
| SCRUM-21 Retención | Los datos no se borran ni se resumen. Triggers de inmutabilidad | Las tablas nuevas no borran nada, y los dos tipos nuevos respetan las reglas de auditoría |
| SCRUM-23 Excel | Exporta lecturas y auditoría | Los eventos de notificación salen en la hoja de auditoría, sin cambios |
| SCRUM-24 Telegram | Envío autónomo de alertas | Usa la migración común, el actor y la sesión de las otras y se prueba junto a ellas |

Una alerta enviada deja constancia, se conserva sin borrarse y puede verse en el Excel exportado.

## Estructuras de datos

La cola es una tabla con índice por estado y fecha. Las solicitudes pasan por estados fijos. Las llaves únicas hacen que la idempotencia y el chat único los garantice la base, no el código. Los códigos se guardan como hash. Un índice único parcial asegura un solo código abierto por usuario.

## Qué pasa si Telegram cae

Las solicitudes quedan como `error_recuperable`. El procesador reintenta con espera. Si Telegram vuelve, el mensaje llega con retraso. Tras 5 intentos fallidos queda `error_permanente` y la auditoría lo registra. La página muestra el estado de la última prueba.

## Preguntas posibles del profesor

**¿Por qué no usó un webhook?** Necesita una URL pública con HTTPS. La demo corre en local. Long polling funciona sin abrir puertos. Cambiar a webhook solo cambia cómo llegan las actualizaciones. La cola y el envío no cambian.

**¿Por qué un proceso aparte y no un hilo del servidor?** Un hilo muere con el servidor y compite con las solicitudes web. Un proceso aparte sigue enviando aunque el servidor se reinicie, y se puede supervisar por separado.

**¿Por qué no RabbitMQ o Redis?** El volumen es bajo y ya hay SQLite. Una tabla con transacciones da persistencia y atomicidad sin otra pieza que instalar. Si el volumen crece, la interfaz permite cambiar la cola sin tocar a SCRUM-25.

**¿Garantiza que el mensaje llega exactamente una vez?** No. Garantiza al menos una vez. Si el procesador muere justo después de que Telegram aceptó el mensaje, se enviará de nuevo. Se prefirió repetir antes que perder una alerta de un bebé.

**¿Qué impide que alguien vincule la cuenta de otra persona?** Necesita el código, que solo ve quien tiene sesión. Vence en 10 minutos, sirve una vez y tiene límite de intentos. Un chat ya vinculado no se puede tomar con otro código.

**¿Qué pasa si un cuidador sin permiso de operar usa Telegram?** Puede vincular el suyo, porque es su cuenta. No puede vincular ni ver el de otro. No opera sobre dispositivos.

**¿Dónde detecta la temperatura peligrosa?** No la detecta. Eso es SCRUM-25. Aquí solo se recibe la solicitud y se envía.

**¿Qué ve el usuario en el mensaje?** Dispositivo, fecha, hora, estado y el título. No el nombre del bebé.

**¿Probó con Telegram de verdad?** No. Solo con un servidor falso que imita la API y con PostgreSQL local. Hay una guía para que el usuario lo haga con su bot.

**¿Funciona con Supabase?** Sí en el código: `Cloud` implementa los mismos métodos que `Local` por RPC y `telegram_worker.py --supabase` elige ese almacén. Se probó el SQL real en PostgreSQL local con los roles de servicio y de usuario, y con un puente HTTP de pruebas. No se probó contra un proyecto real de Supabase, así que no afirmo que esté validada allí.

**¿Dónde queda la clave de servicio?** Solo en el servidor y en el procesador, que son procesos de confianza. El navegador nunca la recibe. Las acciones del usuario usan su propio token y funciones que leen `auth.uid()`. Una prueba comprueba que un usuario autenticado o anónimo recibe 403 al llamar las funciones de servicio.

**¿Qué pasa si Supabase cae?** El procesador lo registra y reintenta cada 5 segundos sin cerrarse. Si rechaza la clave de servicio, se detiene con un aviso, porque reintentar no lo arreglaría.

**¿Cómo sabe que un token no está en GitHub?** Una prueba recorre `cuna`, `web`, `docs`, `supabase` y los archivos de la raíz buscando algo con forma de token. Además `.env` está ignorado y `.env.example` va vacío.

**¿Por qué HTML y no Markdown?** HTML exige escapar tres caracteres. MarkdownV2 exige escapar más de veinte y un olvido hace que Telegram rechace el mensaje.
