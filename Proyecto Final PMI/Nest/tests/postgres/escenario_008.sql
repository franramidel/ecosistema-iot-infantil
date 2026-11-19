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
-- Prueba de la migración 008 (alertas de temperatura y umbrales) contra un PostgreSQL real. Requiere haber corrido antes
-- escenario_005, 006 y 007: usa a 44444444 (admin), 55555555 (cuidador, misma familia) y 66666666 (admin de otra familia).
-- Simula los roles de Supabase. No usar contra producción.
create or replace function pg_temp.expect(ok boolean, label text) returns void language plpgsql as $$ begin if not ok then raise exception 'FALLA: %', label; end if; raise notice 'OK: %', label; end $$;

-- Crea un bebé y un dispositivo asignado desde hace un día (para poder probar lecturas del pasado). Devuelve el bebé.
create or replace function pg_temp.nuevo(dev text, clave text, bebe text) returns uuid language plpgsql as $$
declare k uuid;
begin
 perform set_config('request.jwt.claim.sub', '44444444-4444-4444-4444-444444444444', true);
 k := (public.cuna_change('child', jsonb_build_object('name', bebe))->>'id')::uuid;
 perform public.cuna_change('device', jsonb_build_object('device_id', dev, 'name', 'S', 'secret', clave));
 perform public.cuna_change('assign', jsonb_build_object('device_id', dev, 'expected', null, 'child_id', k));
 update public.assignments set started_at = started_at - interval '1 day' where device_id = dev and ended_at is null;
 return k;
end $$;
create or replace function pg_temp.lec(dev text, clave text, val double precision, metrica text default 'Temperatura',
  unidad text default '°C', antes interval default '0') returns void language plpgsql as $$
begin
 perform public.cuna_ingest(dev, clave, gen_random_uuid(), clock_timestamp() - antes, metrica, val, unidad);
end $$;
create or replace function pg_temp.alertas(dev text) returns bigint language sql as $$
 select count(*) from public.audit_log where device_id = dev and event_type like 'ALERTA_TEMPERATURA%' $$;
create or replace function pg_temp.avisos(dev text) returns bigint language sql as $$
 select count(*) from public.notification_queue q join public.audit_log a on a.id = q.event_id where a.device_id = dev $$;

select family_id as famu from public.members where id='44444444-4444-4444-4444-444444444444' \gset

-- 1. Umbrales por defecto y regla de límites -----------------------------------------------------------------
select pg_temp.nuevo('ALR-1','k1','Bebé uno') as b1 \gset
set role authenticated; set request.jwt.claim.sub = '44444444-4444-4444-4444-444444444444';
select pg_temp.expect((public.cuna_get_thresholds(:'b1')->>'temp_min')::float=19 and (public.cuna_get_thresholds(:'b1')->>'temp_max')::float=22
  and (public.cuna_get_thresholds(:'b1')->>'default')::boolean, 'sin umbrales guardados valen 19 y 22 por defecto');
reset role;
set role anon;
select pg_temp.lec('ALR-1','k1',19.0); select pg_temp.lec('ALR-1','k1',20.5); select pg_temp.lec('ALR-1','k1',22.0);
reset role;
select pg_temp.expect(pg_temp.alertas('ALR-1')=0 and pg_temp.avisos('ALR-1')=0, '19.0, 20.5 y 22.0 (los límites exactos) no alertan');
set role anon; select pg_temp.lec('ALR-1','k1',22.5); reset role;
select pg_temp.expect(pg_temp.alertas('ALR-1')=1, '22.5 supera el máximo y registra una alerta en la auditoría');
select pg_temp.expect((select severity from public.audit_log where device_id='ALR-1' and event_type='ALERTA_TEMPERATURA_ALTA')='critical', 'la alerta es crítica');
select pg_temp.expect(pg_temp.avisos('ALR-1')=2, 'se encola un aviso por cada miembro de la familia (admin y cuidador)');
select pg_temp.expect((select count(*) from public.notification_queue q join public.audit_log a on a.id=q.event_id
   where a.device_id='ALR-1' and q.message like '%Temperatura alta: 22.5 °C (máximo 22 °C)%' and q.message like '%Dispositivo: ALR-1%'
   and q.message like '%Estado: Crítico%')=2, 'el mensaje dice la temperatura, el dispositivo y el estado');
select pg_temp.expect((select count(*) from public.notification_queue where message like '%Bebé uno%')=0, 'el mensaje no lleva el nombre del bebé');
set role anon; select pg_temp.lec('ALR-1','k1',23.0); select pg_temp.lec('ALR-1','k1',24.0); reset role;
select pg_temp.expect(pg_temp.alertas('ALR-1')=1 and pg_temp.avisos('ALR-1')=2, 'seguir fuera de rango no repite el aviso');
set role anon; select pg_temp.lec('ALR-1','k1',21.0); reset role;
select pg_temp.expect(pg_temp.alertas('ALR-1')=1, 'volver a la normalidad no avisa');
set role anon; select pg_temp.lec('ALR-1','k1',23.0); reset role;
select pg_temp.expect(pg_temp.alertas('ALR-1')=2, 'tras normalizarse, un nuevo cruce vuelve a avisar');
set role anon; select pg_temp.lec('ALR-1','k1',18.5); reset role;
select pg_temp.expect((select count(*) from public.audit_log where device_id='ALR-1' and event_type='ALERTA_TEMPERATURA_BAJA')=1, 'de calor a frío es otro cambio de estado');
select pg_temp.expect((select count(*) from public.notification_queue q join public.audit_log a on a.id=q.event_id
   where a.device_id='ALR-1' and q.message like '%Temperatura baja: 18.5 °C (mínimo 19 °C)%')=2, 'el mensaje de frío dice el mínimo');

-- 2. Reintentos, humedad, otras métricas ---------------------------------------------------------------------
select pg_temp.nuevo('ALR-2','k2','Bebé dos') as b2 \gset
select gen_random_uuid() as ev \gset
select clock_timestamp() as t \gset
set role anon;
select public.cuna_ingest('ALR-2','k2',:'ev',:'t','Temperatura',25.0,'°C');
select public.cuna_ingest('ALR-2','k2',:'ev',:'t','Temperatura',25.0,'°C');
select public.cuna_ingest('ALR-2','k2',:'ev',:'t','Temperatura',25.0,'°C');
reset role;
select pg_temp.expect(pg_temp.alertas('ALR-2')=1 and pg_temp.avisos('ALR-2')=2, 'reenviar la misma lectura no duplica la alerta ni los avisos');
select pg_temp.nuevo('ALR-3','k3','Bebé tres') as b3 \gset
set role anon;
select pg_temp.lec('ALR-3','k3',5.0,'Humedad','%'); select pg_temp.lec('ALR-3','k3',99.0,'Humedad','%');
select pg_temp.lec('ALR-3','k3',80.0,'Temperatura','°F'); select pg_temp.lec('ALR-3','k3',80.0,'Peso','g');
reset role;
select pg_temp.expect(pg_temp.alertas('ALR-3')=0, 'la humedad, otras unidades y otras medidas no alertan');
select pg_temp.expect((select count(*) from public.events where device_id='ALR-3')=4, 'esas lecturas sí se guardan');

-- 3. Lectura atrasada ----------------------------------------------------------------------------------------
select pg_temp.nuevo('ALR-4','k4','Bebé cuatro') as b4 \gset
set role anon;
select pg_temp.lec('ALR-4','k4',24.0,antes:='5 seconds');
select pg_temp.lec('ALR-4','k4',20.0,antes:='1 hour');
select pg_temp.lec('ALR-4','k4',24.5);
reset role;
select pg_temp.expect(pg_temp.alertas('ALR-4')=1, 'una lectura atrasada no deshace el estado más reciente: sin aviso repetido');
select pg_temp.expect((select state from cuna_private.alert_state where device_id='ALR-4')='calor', 'el estado sigue en calor');

-- 4. Cambio de umbrales --------------------------------------------------------------------------------------
set role authenticated; set request.jwt.claim.sub = '44444444-4444-4444-4444-444444444444';
select public.cuna_set_thresholds(:'b1', 20, 25);
select pg_temp.expect((public.cuna_get_thresholds(:'b1')->>'temp_max')::float=25 and not (public.cuna_get_thresholds(:'b1')->>'default')::boolean,
  'el administrador cambia los umbrales');
reset role;
select pg_temp.nuevo('ALR-5','k5','Bebé cinco') as b5 \gset
select set_config('t.b5', :'b5', false) \gset
set role authenticated; set request.jwt.claim.sub = '44444444-4444-4444-4444-444444444444';
select public.cuna_set_thresholds(:'b5', 20, 25);
reset role;
set role anon; select pg_temp.lec('ALR-5','k5',24.0); reset role;
select pg_temp.expect(pg_temp.alertas('ALR-5')=0, 'con máximo 25, 24.0 no alerta');
set role anon; select pg_temp.lec('ALR-5','k5',25.5); reset role;
select pg_temp.expect(pg_temp.alertas('ALR-5')=1, 'con máximo 25, 25.5 sí alerta');
select pg_temp.expect((select count(*) from public.notification_queue q join public.audit_log a on a.id=q.event_id
   where a.device_id='ALR-5' and q.message like '%máximo 25 °C%')=2, 'el mensaje usa el máximo configurado');

set role authenticated; set request.jwt.claim.sub = '55555555-5555-5555-5555-555555555555';
do $$ begin perform public.cuna_set_thresholds(current_setting('t.b5')::uuid, 10, 40); raise exception 'no falló';
 exception when sqlstate '42501' then raise notice 'OK: un cuidador no puede cambiar umbrales'; end $$;
select pg_temp.expect((public.cuna_get_thresholds(:'b5')->>'temp_min')::float=20, 'un cuidador sí puede leerlos');
set request.jwt.claim.sub = '66666666-6666-6666-6666-666666666666';
do $$ begin perform public.cuna_get_thresholds(current_setting('t.b5')::uuid); raise exception 'no falló';
 exception when sqlstate 'PT404' then raise notice 'OK: otra familia no ve los umbrales'; end $$;
do $$ begin perform public.cuna_set_thresholds(current_setting('t.b5')::uuid, 10, 40); raise exception 'no falló';
 exception when sqlstate 'PT404' then raise notice 'OK: otra familia no puede cambiarlos'; end $$;
select pg_temp.expect((select count(*) from public.thresholds)=0, 'RLS: otra familia no lee filas de umbrales');
set request.jwt.claim.sub = '44444444-4444-4444-4444-444444444444';
do $$ begin perform public.cuna_set_thresholds(current_setting('t.b5')::uuid, 25, 20); raise exception 'no falló';
 exception when raise_exception then if sqlerrm = 'no falló' then raise; end if; raise notice 'OK: mínimo mayor que máximo se rechaza'; end $$;
do $$ begin perform public.cuna_set_thresholds(current_setting('t.b5')::uuid, 20, 90); raise exception 'no falló';
 exception when raise_exception then if sqlerrm = 'no falló' then raise; end if; raise notice 'OK: valores fuera de -20 a 60 se rechazan'; end $$;
do $$ begin perform public.cuna_set_thresholds(current_setting('t.b5')::uuid, null, 30); raise exception 'no falló';
 exception when raise_exception then if sqlerrm = 'no falló' then raise; end if; raise notice 'OK: un umbral nulo se rechaza'; end $$;
do $$ begin perform public.cuna_set_thresholds(current_setting('t.b5')::uuid, 'NaN'::float, 30); raise exception 'no falló';
 exception when raise_exception then if sqlerrm = 'no falló' then raise; end if; raise notice 'OK: NaN se rechaza'; end $$;
select pg_temp.expect((public.cuna_get_thresholds(:'b5')->>'temp_max')::float=25, 'los intentos inválidos no cambiaron nada');
reset role;
set role anon;
do $$ begin perform public.cuna_set_thresholds(current_setting('t.b5')::uuid, 10, 40); raise exception 'no falló';
 exception when insufficient_privilege then raise notice 'OK: anon no puede cambiar umbrales'; end $$;
reset role;

-- 5. Umbrales para el firmware -------------------------------------------------------------------------------
set role anon;
select pg_temp.expect((public.cuna_device_thresholds('ALR-5','k5')->>'temp_max')::float=25, 'el dispositivo lee los umbrales de su bebé con su clave');
select pg_temp.expect((public.cuna_device_thresholds('ALR-3','k3')->>'temp_min')::float=19, 'sin umbrales guardados el dispositivo recibe 19 y 22');
do $$ begin perform public.cuna_device_thresholds('ALR-5','clave-mala'); raise exception 'no falló';
 exception when sqlstate '42501' then raise notice 'OK: clave de dispositivo incorrecta se rechaza'; end $$;
reset role;
set role authenticated; set request.jwt.claim.sub = '44444444-4444-4444-4444-444444444444';
select public.cuna_change('device','{"device_id":"LIBRE-8","name":"S","secret":"k8"}'::jsonb);
reset role;
set role anon;
do $$ begin perform public.cuna_device_thresholds('LIBRE-8','k8'); raise exception 'no falló';
 exception when sqlstate 'PT409' then raise notice 'OK: un dispositivo sin asignar no recibe umbrales'; end $$;
reset role;

-- 6. Atomicidad: si el aviso falla, la lectura tampoco se guarda --------------------------------------------
select pg_temp.nuevo('ALR-6','k6','Bebé seis') as b6 \gset
select gen_random_uuid() as ev6 \gset
select set_config('t.ev6', :'ev6', false) \gset
insert into public.notification_queue(family_id, member_id, idempotency_key, message, status, next_attempt_at)
 values (:'famu', '44444444-4444-4444-4444-444444444444', 'temp:' || :'ev6' || ':44444444-4444-4444-4444-444444444444', 'otro contenido', 'sin_vinculo', now());
select clock_timestamp() as t6 \gset
set role anon;
do $$ begin perform public.cuna_ingest('ALR-6','k6',current_setting('t.ev6')::uuid,clock_timestamp(),'Temperatura',30.0,'°C'); raise exception 'no falló';
 exception when sqlstate 'PT409' then raise notice 'OK: si el aviso falla se revierte la lectura'; end $$;
reset role;
select pg_temp.expect((select count(*) from public.events where id=:'ev6')=0 and pg_temp.alertas('ALR-6')=0
  and not exists(select 1 from cuna_private.alert_state where device_id='ALR-6'), 'no queda lectura, alerta ni estado a medias');
delete from public.notification_queue where idempotency_key like 'temp:' || :'ev6' || '%';

-- 7. Permisos ----------------------------------------------------------------------------------------------
set role authenticated; set request.jwt.claim.sub = '44444444-4444-4444-4444-444444444444';
do $$ begin perform count(*) from cuna_private.alert_state; raise exception 'no falló';
 exception when insufficient_privilege then raise notice 'OK: el estado de alertas no se lee desde la API'; end $$;
do $$ begin insert into public.thresholds(child_id,family_id,temp_min,temp_max) values (gen_random_uuid(), gen_random_uuid(), 1, 2); raise exception 'no falló';
 exception when insufficient_privilege then raise notice 'OK: la tabla de umbrales no se escribe desde la API'; end $$;
reset role;
-- Ejecutar los cambios de arriba dos veces dejó una sola fila por bebé
select pg_temp.expect((select count(*) from public.thresholds where child_id in (:'b1', :'b5'))=2, 'una fila de umbrales por bebé');
