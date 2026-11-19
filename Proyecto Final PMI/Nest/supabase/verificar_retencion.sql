-- Verificación de solo lectura de la retención permanente. Se puede ejecutar en el editor SQL de
-- Supabase o con psql. Cada fila debe tener ok = true. No modifica ningún dato.
with triggers as (
 select c.relname as tabla, t.tgname as nombre, t.tgtype
 from pg_trigger t join pg_class c on c.oid=t.tgrelid join pg_namespace n on n.oid=c.relnamespace
 where n.nspname='public' and not t.tgisinternal and t.tgenabled <> 'D'
), comprobaciones as (
 select 1 as n, 'events bloquea UPDATE y DELETE' as comprobacion,
  (select count(*) from triggers where tabla='events' and nombre='immutable_event')::text as resultado, '1' as esperado
 union all select 2, 'audit_log bloquea UPDATE y DELETE',
  (select count(*) from triggers where tabla='audit_log' and nombre='audit_immutable')::text, '1'
 union all select 3, 'assignments bloquea DELETE',
  (select count(*) from triggers where tabla='assignments' and nombre='keep_assignments')::text, '1'
 union all select 4, 'TRUNCATE bloqueado en events, audit_log y assignments',
  (select count(*) from triggers where nombre='no_truncate' and (tgtype & 32) = 32)::text, '3'
 union all select 5, 'Tareas pg_cron definidas',
  case when to_regclass('cron.job') is null then '0'
   else (xpath('/row/c/text()', query_to_xml('select count(*) as c from cron.job', false, true, '')))[1]::text end, '0'
 union all select 6, 'Roles de la API con permisos distintos de SELECT sobre el historial',
  (select count(*) from information_schema.role_table_grants
   where table_schema='public' and table_name in ('events','audit_log','assignments')
    and grantee in ('anon','authenticated','PUBLIC') and privilege_type <> 'SELECT')::text, '0'
 union all select 7, 'Lecturas sin renglón de auditoría',
  (select count(*) from public.events e where not exists
   (select 1 from public.audit_log l where l.source_event_id=e.id::text))::text, '0'
 union all select 8, 'Lecturas con dispositivo o bebé inexistente',
  (select count(*) from public.events e where not exists (select 1 from public.devices d where d.id=e.device_id and d.family_id=e.family_id)
    or not exists (select 1 from public.children c where c.id=e.child_id and c.family_id=e.family_id))::text, '0'
 union all select 9, 'Auditoría con dispositivo o bebé inexistente',
  (select count(*) from public.audit_log e where not exists (select 1 from public.devices d where d.id=e.device_id and d.family_id=e.family_id)
    or not exists (select 1 from public.children c where c.id=e.child_id and c.family_id=e.family_id))::text, '0'
 union all select 10, 'Dispositivos sin UUID y auditoría con UUID distinto al de su dispositivo',
  ((select count(*) from public.devices where uuid is null)
   + (select count(*) from public.audit_log l join public.devices d on d.id=l.device_id where l.device_uuid is distinct from d.uuid))::text, '0'
)
select n, comprobacion, resultado, esperado, resultado = esperado as ok from comprobaciones order by n;
