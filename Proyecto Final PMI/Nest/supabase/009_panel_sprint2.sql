-- ESTADO: NO APROBADA PARA DESPLEGAR. NO EJECUTAR EN EL SUPABASE COMPARTIDO SIN AUTORIZACIÓN DEL EQUIPO.
-- Sprint 2: historial ambiental (SCRUM-54), sesión de sueño y snapshot de masa (SCRUM-35), registro de tomas (SCRUM-51),
-- ajustes de luz y audio (SCRUM-57) y panel de control (SCRUM-56). Equivale a cuna/panel.py en la demo SQLite.
-- Depende de 001 (modelo), 003 (audit_log) y 005 (device_uuid). Ejecutar DESPUÉS de 008. No modifica ninguna tabla ni
-- función anterior. Repetirla no cambia nada.
--
-- Todo lo que guardan estas tablas es ESTADO LÓGICO. No existe ninguna ruta de comandos hacia el hardware.
-- Nadie escribe por la API de tablas: solo las funciones de abajo, que toman actor y familia del JWT (auth.uid()).
-- Leer: cualquier miembro de la familia. Operar (tomas, sueño, módulos): can_operate. Ajustes: administradores.
begin;

create table if not exists public.sleep_sessions (
 id uuid primary key default gen_random_uuid(), family_id uuid not null, child_id uuid not null,
 started_at timestamptz not null default clock_timestamp(), started_by uuid references public.members,
 ended_at timestamptz, ended_by uuid references public.members,
 baseline_g double precision check(baseline_g is null or baseline_g > 0),
 baseline_event_id uuid references public.events, baseline_captured_at timestamptz, baseline_at timestamptz,
 check(ended_at is null or ended_at >= started_at),
 foreign key (child_id, family_id) references public.children(id, family_id)
);
create unique index if not exists one_sleep_active on public.sleep_sessions(child_id) where ended_at is null;

create table if not exists public.child_settings (
 child_id uuid primary key, family_id uuid not null,
 led_speed_s double precision not null check(led_speed_s between 0.5 and 60),
 audio text not null check(audio in ('ninguno','ruido_blanco','lluvia','olas','nana')),
 volume integer not null check(volume between 0 and 100),
 updated_at timestamptz not null default clock_timestamp(), updated_by uuid references public.members,
 foreign key (child_id, family_id) references public.children(id, family_id)
);

create table if not exists public.module_states (
 child_id uuid not null, family_id uuid not null,
 module text not null check(module in ('temperatura','luz','audio','colchon')),
 enabled boolean not null, mode text not null check(mode in ('manual','programado')),
 updated_at timestamptz not null default clock_timestamp(), updated_by uuid references public.members,
 primary key (child_id, module),
 foreign key (child_id, family_id) references public.children(id, family_id)
);

create or replace function cuna_private.keep_sleep() returns trigger language plpgsql as $$
begin raise exception 'Sesión de sueño inmutable'; end; $$;
drop trigger if exists immutable_sleep on public.sleep_sessions;
create trigger immutable_sleep before delete on public.sleep_sessions for each row execute function cuna_private.keep_sleep();

do $$
declare t text;
begin
 foreach t in array array['sleep_sessions','child_settings','module_states'] loop
  execute format('alter table public.%I enable row level security', t);
  execute format('revoke all on table public.%I from public, anon, authenticated', t);
  execute format('grant select on table public.%I to authenticated', t);
  execute format('drop policy if exists own_family on public.%I', t);
  execute format('create policy own_family on public.%I for select to authenticated using (family_id = (select cuna_private.family()))', t);
 end loop;
end $$;

-- Actor de una operación sobre un bebé. need: 'read', 'operate' o 'admin'. Primero el permiso y luego el bebé, como en la demo.
create or replace function cuna_private.panel_actor(child uuid, need text) returns public.members
language plpgsql stable security definer set search_path='' as $$
declare actor public.members;
begin
 select * into actor from public.members where id = (select auth.uid());
 if actor.id is null then raise sqlstate '42501' using message='Cuenta sin familia asignada.'; end if;
 if need = 'operate' and not actor.can_operate then raise sqlstate '42501' using message='No tienes permiso para operar dispositivos.'; end if;
 if need = 'admin' and actor.role <> 'admin' then raise sqlstate '42501' using message='Se requiere el rol de administrador.'; end if;
 if not exists (select 1 from public.children where id = child and family_id = actor.family_id)
  then raise sqlstate 'PT404' using message='Perfil no disponible.'; end if;
 return actor;
end; $$;

-- Historial de lecturas con rango de fechas y paginación por cursor (captured_at, id). Pide lim+1 filas.
create or replace function public.cuna_history(child uuid, from_at timestamptz, to_at timestamptz, metric_name text,
 after_at timestamptz, after_id uuid, lim integer) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare actor public.members := cuna_private.panel_actor(child, 'read');
begin
 return coalesce((select jsonb_agg(to_jsonb(e) order by e.captured_at, e.id) from (
   select id, device_id, captured_at, metric, value, unit from public.events
   where child_id = child and (metric_name is null or metric = metric_name)
    and (from_at is null or captured_at >= from_at) and (to_at is null or captured_at < to_at)
    and (after_at is null or (captured_at, id) > (after_at, after_id))
   order by captured_at, id limit least(greatest(coalesce(lim, 500), 1), 2001)) e), '[]'::jsonb);
end; $$;

create or replace function cuna_private.sleep_json(child uuid) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare s public.sleep_sessions; m record; res jsonb;
begin
 select * into s from public.sleep_sessions where child_id = child and ended_at is null;
 select id, value, captured_at into m from public.events
  where child_id = child and metric = 'Masa' and unit = 'g' order by captured_at desc, id desc limit 1;
 res := jsonb_build_object('child_id', child, 'active', s.id is not null, 'session', null, 'mass', null, 'current_pct', null);
 if s.id is not null then
  res := res || jsonb_build_object('session', jsonb_build_object('id', s.id, 'started_at', s.started_at,
   'baseline_g', s.baseline_g, 'baseline_at', s.baseline_at, 'baseline_captured_at', s.baseline_captured_at));
 end if;
 if m.id is not null then
  res := res || jsonb_build_object('mass', jsonb_build_object('value', m.value, 'captured_at', m.captured_at,
   'fresh', m.captured_at >= clock_timestamp() - interval '600 seconds'));
  if s.id is not null and s.baseline_g is not null then
   res := res || jsonb_build_object('current_pct', round((m.value / s.baseline_g * 100)::numeric, 1));
  end if;
 end if;
 return res;
end; $$;

create or replace function public.cuna_sleep_state(child uuid) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare actor public.members := cuna_private.panel_actor(child, 'read');
begin
 return cuna_private.sleep_json(child);
end; $$;

create or replace function public.cuna_sleep_start(child uuid) returns jsonb
language plpgsql security definer set search_path='' as $$
declare actor public.members := cuna_private.panel_actor(child, 'operate');
begin
 perform 1 from public.children where id = child for update;
 if exists (select 1 from public.sleep_sessions where child_id = child and ended_at is null)
  then raise sqlstate 'PT409' using message='Ya hay una sesión de sueño activa.'; end if;
 insert into public.sleep_sessions(family_id, child_id, started_by) values (actor.family_id, child, actor.id);
 update public.families set version = version + 1 where id = actor.family_id;
 return cuna_private.sleep_json(child);
end; $$;

-- Guarda la última lectura de masa (g) como línea base (100 %) de la sesión activa. Sin sesión, sin lectura, con
-- lectura vieja o no positiva responde 409 y no cambia nada. No inventa pesos.
create or replace function public.cuna_sleep_snapshot(child uuid) returns jsonb
language plpgsql security definer set search_path='' as $$
declare actor public.members := cuna_private.panel_actor(child, 'operate'); s public.sleep_sessions; m record;
begin
 select * into s from public.sleep_sessions where child_id = child and ended_at is null for update;
 if s.id is null then raise sqlstate 'PT409' using message='No hay una sesión de sueño activa. Inicia una antes de tomar el snapshot.'; end if;
 select id, value, captured_at into m from public.events
  where child_id = child and metric = 'Masa' and unit = 'g' order by captured_at desc, id desc limit 1;
 if m.id is null then raise sqlstate 'PT409' using message='Aún no hay lecturas de masa en gramos (g) para este bebé.'; end if;
 if m.captured_at < clock_timestamp() - interval '600 seconds'
  then raise sqlstate 'PT409' using message='La última lectura de masa tiene más de 10 minutos. Revisa el colchón y vuelve a intentarlo.'; end if;
 if not m.value > 0 then raise sqlstate 'PT409' using message='La lectura de masa no es válida: debe ser mayor que 0 g.'; end if;
 update public.sleep_sessions set baseline_g = m.value, baseline_event_id = m.id, baseline_captured_at = m.captured_at,
  baseline_at = clock_timestamp() where id = s.id;
 update public.families set version = version + 1 where id = actor.family_id;
 return cuna_private.sleep_json(child);
end; $$;

create or replace function public.cuna_sleep_disarm(child uuid) returns jsonb
language plpgsql security definer set search_path='' as $$
declare actor public.members := cuna_private.panel_actor(child, 'operate'); s public.sleep_sessions;
begin
 select * into s from public.sleep_sessions where child_id = child and ended_at is null for update;
 if s.id is null then raise sqlstate 'PT409' using message='No hay una sesión de sueño activa.'; end if;
 update public.sleep_sessions set ended_at = clock_timestamp(), ended_by = actor.id where id = s.id;
 update public.families set version = version + 1 where id = actor.family_id;
 return cuna_private.sleep_json(child);
end; $$;

-- Toma de alimento. Se guarda en audit_log como evento TOMA, ligada al dispositivo activo del bebé (audit_log exige
-- dispositivo). Idempotente por event_id: reintentar no duplica. El instante lo pone la base al recibir la toma.
create or replace function public.cuna_log_feeding(child uuid, event_id uuid, duration_s integer, volume double precision,
 unit text) returns jsonb
language plpgsql security definer set search_path='' as $$
declare actor public.members := cuna_private.panel_actor(child, 'operate'); prior public.audit_log; dev text; new_id uuid;
 h integer; mi integer; se integer; detalle text;
begin
 if event_id is null then raise exception 'Se requiere un event_id UUID.'; end if;
 if duration_s is null or duration_s < 1 or duration_s > 21600 then raise exception 'La duración debe estar entre 1 segundo y 6 horas.'; end if;
 if volume is null or volume <> volume or not volume > 0 or volume > 2000 then raise exception 'El volumen debe ser mayor que 0 y de hasta 2000.'; end if;
 if unit is null or unit not in ('g', 'ml') then raise exception 'La unidad del volumen debe ser g o ml.'; end if;
 select * into prior from public.audit_log where source_event_id = event_id::text;
 if found then
  if prior.child_id <> child or prior.event_type <> 'TOMA' or prior.family_id <> actor.family_id
   then raise sqlstate 'PT409' using message='El identificador del evento ya tiene otro contenido.'; end if;
  return jsonb_build_object('child_id', child, 'id', prior.id, 'device_id', prior.device_id, 'duplicado', true);
 end if;
 select device_id into dev from public.assignments where child_id = child and ended_at is null order by device_id limit 1;
 if dev is null then raise sqlstate 'PT409' using message='Asigna un dispositivo al bebé antes de registrar tomas: la auditoría guarda cada evento con su dispositivo.'; end if;
 h := duration_s / 3600; mi := (duration_s % 3600) / 60; se := duration_s % 60;
 detalle := 'Toma: ' || case when h > 0 then h || ' h ' || lpad(mi::text, 2, '0') || ' min ' || lpad(se::text, 2, '0') || ' s'
   when mi > 0 then mi || ' min ' || lpad(se::text, 2, '0') || ' s' else se || ' s' end
   || ' · ' || to_char(round(volume::numeric, 1), 'FM9999990.0') || ' ' || unit;
 insert into public.audit_log(family_id, occurred_at, event_type, detail, child_id, device_id, severity, source_event_id)
  values (actor.family_id, clock_timestamp(), 'TOMA', detalle, child, dev, 'info', event_id::text) returning id into new_id;
 update public.families set version = version + 1 where id = actor.family_id;
 return jsonb_build_object('child_id', child, 'id', new_id, 'device_id', dev, 'duplicado', false);
end; $$;

create or replace function cuna_private.settings_json(child uuid) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare s public.child_settings;
begin
 select * into s from public.child_settings where child_id = child;
 if found then
  return jsonb_build_object('child_id', child, 'led_speed_s', s.led_speed_s, 'audio', s.audio, 'volume', s.volume, 'default', false);
 end if;
 return jsonb_build_object('child_id', child, 'led_speed_s', 5, 'audio', 'ruido_blanco', 'volume', 30, 'default', true);
end; $$;

create or replace function public.cuna_get_settings(child uuid) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare actor public.members := cuna_private.panel_actor(child, 'read');
begin
 return cuna_private.settings_json(child);
end; $$;

create or replace function public.cuna_set_settings(child uuid, led double precision, audio_name text, vol integer) returns jsonb
language plpgsql security definer set search_path='' as $$
declare actor public.members := cuna_private.panel_actor(child, 'admin');
begin
 if led is null or led <> led or led < 0.5 or led > 60 then raise exception 'La velocidad del LED va de 0.5 a 60 segundos.'; end if;
 if audio_name is null or audio_name not in ('ninguno', 'ruido_blanco', 'lluvia', 'olas', 'nana') then raise exception 'Elige un audio de la lista.'; end if;
 if vol is null or vol < 0 or vol > 100 then raise exception 'El volumen va de 0 a 100.'; end if;
 insert into public.child_settings(child_id, family_id, led_speed_s, audio, volume, updated_at, updated_by)
  values (child, actor.family_id, led, audio_name, vol, clock_timestamp(), actor.id)
  on conflict (child_id) do update set led_speed_s = excluded.led_speed_s, audio = excluded.audio, volume = excluded.volume,
   updated_at = excluded.updated_at, updated_by = excluded.updated_by;
 update public.families set version = version + 1 where id = actor.family_id;
 return cuna_private.settings_json(child);
end; $$;

create or replace function cuna_private.modules_json(child uuid) returns jsonb
language sql stable security definer set search_path='' as $$
 select jsonb_build_object('child_id', child, 'modules', (
  select jsonb_agg(jsonb_build_object('module', m.name, 'enabled', coalesce(s.enabled, m.init),
   'mode', coalesce(s.mode, 'manual'), 'default', s.module is null) order by m.ord)
  from (values ('temperatura', true, 1), ('luz', false, 2), ('audio', false, 3), ('colchon', false, 4)) as m(name, init, ord)
  left join public.module_states s on s.child_id = child and s.module = m.name));
$$;

create or replace function public.cuna_get_modules(child uuid) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare actor public.members := cuna_private.panel_actor(child, 'read');
begin
 return cuna_private.modules_json(child);
end; $$;

create or replace function public.cuna_set_module(child uuid, module_name text, on_off boolean, mode_name text) returns jsonb
language plpgsql security definer set search_path='' as $$
declare actor public.members := cuna_private.panel_actor(child, 'operate');
begin
 if module_name is null or module_name not in ('temperatura', 'luz', 'audio', 'colchon') then raise exception 'Módulo desconocido.'; end if;
 if on_off is null then raise exception 'El estado del módulo debe ser booleano.'; end if;
 if mode_name is null or mode_name not in ('manual', 'programado') then raise exception 'El modo debe ser manual o programado.'; end if;
 insert into public.module_states(child_id, family_id, module, enabled, mode, updated_at, updated_by)
  values (child, actor.family_id, module_name, on_off, mode_name, clock_timestamp(), actor.id)
  on conflict (child_id, module) do update set enabled = excluded.enabled, mode = excluded.mode,
   updated_at = excluded.updated_at, updated_by = excluded.updated_by;
 update public.families set version = version + 1 where id = actor.family_id;
 return cuna_private.modules_json(child);
end; $$;

revoke all on function cuna_private.keep_sleep(), cuna_private.panel_actor(uuid, text), cuna_private.sleep_json(uuid),
 cuna_private.settings_json(uuid), cuna_private.modules_json(uuid) from public, anon, authenticated;
revoke all on function public.cuna_history(uuid, timestamptz, timestamptz, text, timestamptz, uuid, integer),
 public.cuna_sleep_state(uuid), public.cuna_sleep_start(uuid), public.cuna_sleep_snapshot(uuid), public.cuna_sleep_disarm(uuid),
 public.cuna_log_feeding(uuid, uuid, integer, double precision, text), public.cuna_get_settings(uuid),
 public.cuna_set_settings(uuid, double precision, text, integer), public.cuna_get_modules(uuid),
 public.cuna_set_module(uuid, text, boolean, text) from public, anon, authenticated;
grant execute on function public.cuna_history(uuid, timestamptz, timestamptz, text, timestamptz, uuid, integer),
 public.cuna_sleep_state(uuid), public.cuna_sleep_start(uuid), public.cuna_sleep_snapshot(uuid), public.cuna_sleep_disarm(uuid),
 public.cuna_log_feeding(uuid, uuid, integer, double precision, text), public.cuna_get_settings(uuid),
 public.cuna_set_settings(uuid, double precision, text, integer), public.cuna_get_modules(uuid),
 public.cuna_set_module(uuid, text, boolean, text) to authenticated;
commit;
