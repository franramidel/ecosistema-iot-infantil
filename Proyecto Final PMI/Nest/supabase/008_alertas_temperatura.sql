-- ESTADO: NO APROBADA PARA DESPLEGAR. NO EJECUTAR EN EL SUPABASE COMPARTIDO SIN AUTORIZACIÓN DEL EQUIPO.
-- RF-NOT-02, RF-CUN-01 y RF-CUN-02. Equivale a cuna/alertas.py en la demo SQLite.
-- Agrega un trigger sobre public.events (modelo de Sebastián), igual que 003. Depende de 003 (audit_log) y 007 (cuna_notify).
-- Ejecutar DESPUÉS de 007. No modifica ninguna tabla ni función anterior. Repetirla no cambia nada.
--
-- Regla del SRS: rango por defecto 19 a 22 °C. Alerta solo si se SUPERA el máximo o se baja del mínimo (> y <).
-- Se avisa únicamente al CAMBIAR de estado hacia fuera de rango. Volver a la normalidad no avisa. La humedad no alerta.
-- El trigger corre en la misma transacción que la lectura: si falla el aviso, la lectura tampoco se guarda y el dispositivo
-- la reenvía con el mismo UUID (cuna_ingest es idempotente), así que nunca queda una lectura crítica sin aviso.
begin;

create table if not exists public.thresholds (
 child_id uuid primary key, family_id uuid not null,
 temp_min double precision not null, temp_max double precision not null,
 updated_at timestamptz not null default now(), updated_by uuid references public.members,
 check(temp_min < temp_max),
 check(temp_min between -20 and 60 and temp_max between -20 and 60),
 foreign key (child_id, family_id) references public.children(id, family_id)
);
create table if not exists cuna_private.alert_state (
 child_id uuid not null, device_id text not null references public.devices,
 state text not null check(state in ('normal','calor','frio')), since timestamptz not null,
 primary key (child_id, device_id)
);

alter table public.thresholds enable row level security;
revoke all on public.thresholds from public, anon, authenticated;
revoke all on cuna_private.alert_state from public, anon, authenticated;
grant select on public.thresholds to authenticated;
drop policy if exists family_thresholds on public.thresholds;
create policy family_thresholds on public.thresholds for select to authenticated using (family_id = (select cuna_private.family()));

-- Lectura de umbrales: cualquier miembro de la familia. Sin fila guardada devuelve los valores por defecto.
create or replace function public.cuna_get_thresholds(child uuid) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare fam uuid := cuna_private.family(); t public.thresholds;
begin
 if fam is null or not exists (select 1 from public.children where id = child and family_id = fam)
  then raise sqlstate 'PT404' using message='Perfil no disponible.'; end if;
 select * into t from public.thresholds where child_id = child;
 if found then return jsonb_build_object('child_id', child, 'temp_min', t.temp_min, 'temp_max', t.temp_max, 'default', false); end if;
 return jsonb_build_object('child_id', child, 'temp_min', 19, 'temp_max', 22, 'default', true);
end; $$;

-- Cambio de umbrales: solo administradores.
create or replace function public.cuna_set_thresholds(child uuid, tmin double precision, tmax double precision) returns jsonb
language plpgsql security definer set search_path='' as $$
declare fam uuid := cuna_private.family();
begin
 if fam is null or not exists (select 1 from public.children where id = child and family_id = fam)
  then raise sqlstate 'PT404' using message='Perfil no disponible.'; end if;
 if not cuna_private.is_admin() then raise sqlstate '42501' using message='Se requiere el rol de administrador.'; end if;
 if tmin is null or tmax is null or tmin <> tmin or tmax <> tmax or tmin = 'Infinity' or tmax = 'Infinity' or tmin = '-Infinity' or tmax = '-Infinity'
  then raise exception 'Los umbrales deben ser números.'; end if;
 if tmin < -20 or tmin > 60 or tmax < -20 or tmax > 60 then raise exception 'Los umbrales deben estar entre -20 y 60 °C.'; end if;
 if not tmin < tmax then raise exception 'El mínimo debe ser menor que el máximo.'; end if;
 insert into public.thresholds(child_id, family_id, temp_min, temp_max, updated_at, updated_by)
  values (child, fam, tmin, tmax, now(), (select auth.uid()))
  on conflict (child_id) do update set temp_min = excluded.temp_min, temp_max = excluded.temp_max,
   updated_at = excluded.updated_at, updated_by = excluded.updated_by;
 update public.families set version = version + 1 where id = fam;
 return jsonb_build_object('child_id', child, 'temp_min', tmin, 'temp_max', tmax, 'default', false);
end; $$;

-- Umbrales para el firmware: el dispositivo se autentica con su clave, igual que en cuna_ingest. No revela quién es el bebé.
create or replace function public.cuna_device_thresholds(device text, secret text) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare fam uuid; kid uuid; t public.thresholds;
begin
 select d.family_id into fam from public.devices d join cuna_private.device_keys k on k.device_id = d.id
  where d.id = device and k.digest = encode(sha256(convert_to(secret,'UTF8')),'hex');
 if fam is null then raise sqlstate '42501' using message='Credencial de dispositivo inválida.'; end if;
 select child_id into kid from public.assignments where device_id = device and started_at <= clock_timestamp()
  and (ended_at is null or clock_timestamp() < ended_at);
 if kid is null then raise sqlstate 'PT409' using message='El dispositivo no está asignado a ningún bebé.'; end if;
 select * into t from public.thresholds where child_id = kid;
 if found then return jsonb_build_object('temp_min', t.temp_min, 'temp_max', t.temp_max); end if;
 return jsonb_build_object('temp_min', 19, 'temp_max', 22);
end; $$;

-- Evalúa cada lectura de temperatura en °C. Corre después de insertar en events, en la misma transacción.
create or replace function cuna_private.alert_temperature() returns trigger
language plpgsql security definer set search_path='' as $$
declare tmin double precision := 19; tmax double precision := 22; nuevo text; prev cuna_private.alert_state;
 tipo text; titulo text; audit uuid; miembro record; msg text; local_at timestamp;
begin
 if new.metric <> 'Temperatura' or new.unit <> '°C' then return new; end if;
 select temp_min, temp_max into tmin, tmax from public.thresholds where child_id = new.child_id;
 if not found then tmin := 19; tmax := 22; end if;
 nuevo := case when new.value > tmax then 'calor' when new.value < tmin then 'frio' else 'normal' end;
 select * into prev from cuna_private.alert_state where child_id = new.child_id and device_id = new.device_id for update;
 if found and new.captured_at < prev.since then return new; end if;  -- lectura atrasada: no deshace un estado más reciente
 if not found then
  insert into cuna_private.alert_state values (new.child_id, new.device_id, nuevo, new.captured_at);
  prev.state := 'normal';
 elsif prev.state <> nuevo then
  update cuna_private.alert_state set state = nuevo, since = new.captured_at where child_id = new.child_id and device_id = new.device_id;
 end if;
 if nuevo = prev.state or nuevo = 'normal' then return new; end if;
 tipo := case when nuevo = 'calor' then 'ALERTA_TEMPERATURA_ALTA' else 'ALERTA_TEMPERATURA_BAJA' end;
 titulo := case when nuevo = 'calor' then 'Temperatura alta: ' || to_char(new.value::numeric, 'FM990.0') || ' °C (máximo ' || to_char(tmax::numeric, 'FM990.##') || ' °C)'
  else 'Temperatura baja: ' || to_char(new.value::numeric, 'FM990.0') || ' °C (mínimo ' || to_char(tmin::numeric, 'FM990.##') || ' °C)' end;
 insert into public.audit_log(family_id, occurred_at, event_type, detail, child_id, device_id, severity, source_event_id)
  values (new.family_id, new.captured_at, tipo, titulo, new.child_id, new.device_id, 'critical', 'alerta:' || new.id::text)
  returning id into audit;
 local_at := new.captured_at at time zone 'America/Costa_Rica';
 msg := E'<b>Ecosistema IoT Infantil</b>\n\nAlerta: ' || titulo || E'\nDispositivo: ' || new.device_id ||
  E'\nFecha: ' || to_char(local_at, 'DD/MM/YYYY') || E'\nHora: ' || to_char(local_at, 'HH24:MI') || E'\nEstado: Crítico';
 for miembro in select id from public.members where family_id = new.family_id order by id loop
  perform public.cuna_notify(miembro.id, 'temp:' || new.id::text || ':' || miembro.id::text, msg, audit);
 end loop;
 return new;
end; $$;
drop trigger if exists alert_temperature on public.events;
create trigger alert_temperature after insert on public.events for each row execute function cuna_private.alert_temperature();

revoke all on function cuna_private.alert_temperature() from public, anon, authenticated;
revoke all on function public.cuna_get_thresholds(uuid), public.cuna_set_thresholds(uuid,double precision,double precision),
 public.cuna_device_thresholds(text,text) from public, anon, authenticated;
grant execute on function public.cuna_get_thresholds(uuid), public.cuna_set_thresholds(uuid,double precision,double precision) to authenticated;
grant execute on function public.cuna_device_thresholds(text,text) to anon, authenticated;
commit;
