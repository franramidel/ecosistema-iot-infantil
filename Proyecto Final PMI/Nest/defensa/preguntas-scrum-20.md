# Defensa de SCRUM-20. Preguntas posibles

Material personal para la defensa. No es documentación de integración.

**¿Por qué una tabla aparte y no ampliar `events`?** `events` es telemetría de la Persona 1, inmutable y con otro propósito. La auditoría cubre más hechos, como asignaciones. Una tabla aparte no rompe nada y permite un modelo propio.

**¿Cómo garantiza que el evento queda con el bebé correcto?** El bebé no se recibe del dispositivo. Se busca en `assignments` el intervalo que contiene `occurred_at`. Un índice único evita dos asignaciones abiertas por dispositivo.

**¿Qué pasa si el traslado ocurre mientras llegan eventos?** SQLite usa transacciones `begin immediate`, así que las escrituras se serializan. Hay una prueba con 24 envíos concurrentes durante un traslado. Todos quedan con el bebé que tenía el dispositivo cuando ocurrieron.

**¿Por qué triggers y no código en cada módulo?** Un trigger no se puede olvidar. Cubre incluso el código que escriba otro compañero, y comparte la transacción con el hecho original.

**¿Cómo evita SQL injection?** Todos los valores van como parámetros. Los nombres de columna del filtro salen de una lista fija en el código, no de la solicitud.

**¿Qué estructura de datos usa la paginación?** Un cursor por clave compuesta, instante y orden de inserción. Es más estable que `offset` cuando entran eventos nuevos mientras se pagina.
