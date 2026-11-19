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
-- Prueba de la migración 006 (vista audit_export) contra un PostgreSQL real. Requiere haber corrido
-- antes escenario_005.sql, cuyos datos usa (familia de uuid.padre@example.test, TEMP-01 con una lectura).
-- Simula los roles de Supabase. No usar contra producción.
create or replace function pg_temp.expect(ok boolean, label text) returns void language plpgsql as $$ begin if not ok then raise exception 'FALLA: %', label; end if; raise notice 'OK: %', label; end $$;

select family_id as famu from public.members where id='44444444-4444-4444-4444-444444444444' \gset
insert into public.families default values returning id as famx \gset
insert into auth.users(id,email) values ('55555555-5555-5555-5555-555555555555','uuid.cuida@example.test'),('66666666-6666-6666-6666-666666666666','uuid.otra@example.test');
insert into public.members values ('55555555-5555-5555-5555-555555555555',:'famu','uuid.cuida@example.test','caregiver',true),
 ('66666666-6666-6666-6666-666666666666',:'famx','uuid.otra@example.test','admin',true);

set role authenticated; set request.jwt.claim.sub = '66666666-6666-6666-6666-666666666666';
select (public.cuna_change('child','{"name":"Ajeno"}'::jsonb)->>'id') as ajeno \gset
select public.cuna_change('device','{"device_id":"AJENO-6","name":"S","secret":"kx"}'::jsonb);
select public.cuna_change('assign', jsonb_build_object('device_id','AJENO-6','expected',null,'child_id',:'ajeno'));
reset role;
select clock_timestamp() as t1 \gset
set role anon; select public.cuna_ingest('AJENO-6','kx','aaaaaaaa-0000-4000-8000-0000000000b1',:'t1','Temperatura',99.9,'°C'); reset role;

set role authenticated; set request.jwt.claim.sub = '44444444-4444-4444-4444-444444444444';
select pg_temp.expect((select count(*) from public.audit_export where hoja='telemetria' and device_id='TEMP-01')=1, 'el administrador ve su lectura en la hoja de telemetría');
select pg_temp.expect((select metric||'|'||value||'|'||unit from public.audit_export where hoja='telemetria' and device_id='TEMP-01')='Temperatura|21.5|°C', 'medida, valor y unidad vienen de events');
select pg_temp.expect((select count(*) from public.audit_export where hoja='auditoria' and event_type='LECTURA')=0, 'las lecturas no aparecen en la hoja de auditoría');
select pg_temp.expect((select count(*) from public.audit_export)=(select count(*) from public.audit_log), 'la vista no repite ni pierde filas respecto de audit_log');
select pg_temp.expect((select count(*) from public.audit_export where device_id='AJENO-6')=0, 'no se ven registros de otra familia');
select pg_temp.expect((select count(*) from public.audit_export where device_uuid is null)=0, 'toda fila trae el UUID del dispositivo');
reset role;

set role authenticated; set request.jwt.claim.sub = '55555555-5555-5555-5555-555555555555';
select pg_temp.expect((select count(*) from public.audit_export)=0, 'un cuidador no ve nada (RLS de audit_log)');
reset role;

set role authenticated; set request.jwt.claim.sub = '66666666-6666-6666-6666-666666666666';
select pg_temp.expect((select count(*) from public.audit_export where device_id='AJENO-6')=2 and (select count(*) from public.audit_export where device_id<>'AJENO-6')=0, 'otra familia ve solo lo suyo');
reset role;

do $$ begin
 set local role anon;
 begin perform count(*) from public.audit_export; raise exception 'no falló';
 exception when insufficient_privilege then raise notice 'OK: anon no puede leer la vista'; end;
 reset role;
 begin insert into public.audit_export(id) values (gen_random_uuid()); raise exception 'no falló';
 exception when others then raise notice 'OK: la vista no admite escrituras'; end;
end $$;
select pg_temp.expect((select (reloptions)::text from pg_class where relname='audit_export')::text like '%security_invoker=true%', 'vista con security_invoker');
