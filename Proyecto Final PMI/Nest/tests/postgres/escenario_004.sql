-- ADVERTENCIA: SOLO PARA UNA BASE POSTGRESQL LOCAL Y DESECHABLE.
-- Este archivo inserta usuarios y datos falsos y no debe correr nunca sobre el Supabase compartido del equipo
-- ni sobre producción. Se niega a correr si la sesión no declara que es un entorno desechable
-- (tests/postgres/ejecutar.sh y las pruebas automáticas lo declaran por sí mismos).
\set ON_ERROR_STOP 1
do $$ begin
 if coalesce(current_setting('cuna.entorno_desechable', true), '') <> 'si' then
  raise exception 'ABORTADO: este script es solo para una base local desechable, nunca para Supabase compartido o producción.';
 end if;
end $$;
-- Prueba de la migración 004: el historial no se puede vaciar ni borrar, ni siquiera como superusuario.
create or replace function pg_temp.expect(ok boolean, label text) returns void language plpgsql as $$ begin if not ok then raise exception 'FALLA: %', label; end if; raise notice 'OK: %', label; end $$;
select count(*) as eventos_antes from public.events \gset
select count(*) as auditoria_antes from public.audit_log \gset
select count(*) as asignaciones_antes from public.assignments \gset
do $$
declare t text;
begin
 foreach t in array array['events','audit_log','assignments'] loop
  begin execute 'truncate public.' || t; raise exception 'no falló';
  exception when raise_exception then
   if sqlerrm <> 'Los registros históricos no se pueden vaciar' then raise; end if;
   raise notice 'OK: TRUNCATE % bloqueado', t;
  end;
 end loop;
 begin truncate public.families cascade; raise exception 'no falló';
 exception when raise_exception then
  if sqlerrm <> 'Los registros históricos no se pueden vaciar' then raise; end if;
  raise notice 'OK: TRUNCATE ... CASCADE desde families bloqueado';
 end;
 begin delete from public.assignments; raise exception 'no falló';
 exception when raise_exception then
  if sqlerrm <> 'Evento inmutable' then raise; end if;
  raise notice 'OK: DELETE sobre assignments bloqueado';
 end;
 begin delete from public.events; raise exception 'no falló';
 exception when raise_exception then
  if sqlerrm <> 'Evento inmutable' then raise; end if;
  raise notice 'OK: DELETE sobre events bloqueado';
 end;
end $$;
select pg_temp.expect((select count(*) from public.events)=:eventos_antes, 'events conserva todas sus filas');
select pg_temp.expect((select count(*) from public.audit_log)=:auditoria_antes, 'audit_log conserva todas sus filas');
select pg_temp.expect((select count(*) from public.assignments)=:asignaciones_antes, 'assignments conserva todas sus filas');
