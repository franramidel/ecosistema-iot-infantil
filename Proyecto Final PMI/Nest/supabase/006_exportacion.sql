-- ESTADO: NO APROBADA PARA DESPLEGAR. Solo crea una vista de lectura, pero lee audit_log y events y necesita
-- las migraciones 003 y 005, que esperan aprobación. NO EJECUTAR EN EL SUPABASE COMPARTIDO SIN AUTORIZACIÓN.
-- SCRUM-23 · RF-DAT-04. Vista de apoyo para la exportación a Excel. Ejecutar DESPUÉS de 005.
-- Une cada lectura de audit_log con su medida, valor y unidad en events, y marca a qué hoja pertenece
-- cada fila: las lecturas van a 'telemetria' y todo lo demás a 'auditoria'. Así la exportación pagina
-- una sola fuente y ningún hecho se exporta dos veces.
-- security_invoker hace que la vista use los permisos y la RLS de quien consulta: solo los
-- administradores de la familia ven filas (política admin_reads_audit). No crea ni modifica datos.
begin;

create or replace view public.audit_export with (security_invoker = true) as
select l.id, l.seq, l.family_id, l.occurred_at, l.event_date, l.event_time, l.event_type, l.detail,
       l.child_id, l.device_id, l.device_uuid, l.severity,
       case when l.event_type = 'LECTURA' then 'telemetria' else 'auditoria' end as hoja,
       e.metric, e.value, e.unit
from public.audit_log l
left join public.events e on l.event_type = 'LECTURA' and e.id::text = l.source_event_id;

revoke all on table public.audit_export from public, anon, authenticated;
grant select on table public.audit_export to authenticated;

commit;
