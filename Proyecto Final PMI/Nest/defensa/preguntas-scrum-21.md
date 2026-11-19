# Defensa de SCRUM-21. Preguntas posibles

Material personal para la defensa. No es documentación de integración.

**¿Cómo garantizan que nadie borre datos?** En dos capas. El código no tiene ninguna sentencia de borrado, y una prueba lo revisa en cada ejecución. Además la base rechaza UPDATE, DELETE y TRUNCATE con triggers, de modo que ni un error futuro lo permitiría.

**Si todo es inmutable, ¿cómo cumplen el derecho a la supresión?** Con un procedimiento manual y excepcional. La inmutabilidad protege contra borrados accidentales y automáticos, pero no impide que un administrador autorizado atienda una solicitud legal.

**¿Por qué usan la API de respaldo de SQLite y no copiar el archivo?** Copiar un archivo mientras alguien escribe puede dar una copia a medias. La API toma una fotografía consistente. Hay una prueba que respalda tres veces mientras otro hilo inserta lecturas sin parar.

**¿Cómo saben que un respaldo sirve?** Se verifica al crearlo y otra vez al restaurarlo, y se compara tabla por tabla con el original. Un respaldo que nunca se restauró no se puede considerar probado.

**¿Qué pasa si se corta la luz durante una escritura?** SQLite revierte la transacción incompleta al reabrir el archivo. El servidor responde 500 al dispositivo, que debe reintentar con el mismo UUID. La lectura queda una sola vez gracias al identificador.

**¿Qué diferencia hay entre retención y respaldo?** La retención es no borrar. El respaldo es tener otra copia por si el disco falla. Se necesitan las dos.

**¿Por qué TRUNCATE necesita su propio trigger?** Porque PostgreSQL no activa los triggers de fila al vaciar una tabla con TRUNCATE. Hay un trigger distinto, de sentencia, para ese caso.
