# Defensa de SCRUM-23. Preguntas posibles

Material personal para la defensa. No es documentación de integración.

**¿Por qué dos hojas de datos?** Una lectura y un evento de auditoría tienen columnas distintas. Separarlas evita celdas vacías y evita exportar la lectura dos veces.

**¿Cómo evita la inyección de fórmulas?** Prefija con comilla los textos peligrosos, los guarda como texto y la prueba revisa todo el libro. Los números salen como números y nunca desde texto del usuario.

**¿Qué pasa con un millón de filas?** La memoria no crece con las filas. El límite real es el de Excel, 1 048 575 por hoja, y se informa con 413.

**¿Cómo evita que un administrador vea otra familia?** Cada consulta filtra por la familia de la sesión. Los filtros se resuelven dentro de esa familia, y en Supabase además actúa la RLS.

**¿Qué pasa si llegan eventos mientras se exporta?** Quedan fuera por el corte fijado al inicio. El archivo es una foto coherente.

**¿Por qué no generar el Excel en el navegador?** Obligaría a enviar todos los datos al cliente y a confiar en él. El servidor autoriza, filtra y limita.
