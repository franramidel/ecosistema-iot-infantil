-- Ejecutar una vez en un proyecto Supabase vacío con el editor SQL.
begin;
create schema if not exists cuna_private;
revoke all on schema cuna_private from public, anon, authenticated;

create table public.families (
 id uuid primary key default gen_random_uuid(), version bigint not null default 0
);
create table public.members (
 id uuid primary key references auth.users(id), family_id uuid not null references public.families,
 email text not null, role text not null check(role in ('admin','caregiver')),
 can_operate boolean not null, check(role <> 'admin' or can_operate)
);
create index members_family on public.members(family_id);
create table public.children (
 id uuid primary key default gen_random_uuid(), family_id uuid not null references public.families,
 name text not null check(length(trim(name)) between 1 and 80), unique(id,family_id)
);
create table public.devices (
 id text primary key check(id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$'),
 family_id uuid not null references public.families, name text not null check(length(trim(name)) between 1 and 80),
 unique(id,family_id)
);
create table cuna_private.device_keys (
 device_id text primary key references public.devices, digest text not null
);
create table public.assignments (
 id uuid primary key default gen_random_uuid(), family_id uuid not null,
 device_id text not null, child_id uuid not null, started_at timestamptz not null,
 ended_at timestamptz, enabled boolean not null default false,
 foreign key (device_id,family_id) references public.devices(id,family_id),
 foreign key (child_id,family_id) references public.children(id,family_id),
 check(ended_at is null or ended_at >= started_at)
);
create unique index one_active_device on public.assignments(device_id) where ended_at is null;
create index assignment_period on public.assignments(device_id,started_at);
create table public.events (
 id uuid primary key, family_id uuid not null, device_id text not null, child_id uuid not null,
 captured_at timestamptz not null, metric text not null check(length(trim(metric)) between 1 and 40),
 value double precision not null check(value > '-Infinity'::float8 and value < 'Infinity'::float8),
 unit text not null check(length(trim(unit)) between 1 and 16),
 foreign key (device_id,family_id) references public.devices(id,family_id),
 foreign key (child_id,family_id) references public.children(id,family_id)
);
create index child_timeline on public.events(child_id,captured_at desc,id desc);
create function cuna_private.keep_history() returns trigger language plpgsql as $$
begin raise exception 'Evento inmutable'; end; $$;
create trigger immutable_event before update or delete on public.events for each row execute function cuna_private.keep_history();

create function cuna_private.family() returns uuid language sql stable security definer set search_path='' as $$
 select family_id from public.members where id=(select auth.uid());
$$;
create function cuna_private.is_admin() returns boolean language sql stable security definer set search_path='' as $$
 select exists(select 1 from public.members where id=(select auth.uid()) and role='admin');
$$;
grant usage on schema cuna_private to authenticated;
revoke all on cuna_private.device_keys from public, anon, authenticated;
revoke all on all functions in schema cuna_private from public, anon, authenticated;
grant execute on function cuna_private.family(), cuna_private.is_admin() to authenticated;

do $$
declare t text;
begin
 foreach t in array array['families','members','children','devices','assignments','events'] loop
  execute format('alter table public.%I enable row level security',t);
  execute format('revoke all on table public.%I from public, anon, authenticated',t);
  execute format('grant select on table public.%I to authenticated',t);
 end loop;
 foreach t in array array['children','devices','assignments','events'] loop
  execute format('create policy own_family on public.%I for select to authenticated using (family_id=(select cuna_private.family()))',t);
 end loop;
end $$;
create policy own_family on public.families for select to authenticated using(id=(select cuna_private.family()));
create policy member_visibility on public.members for select to authenticated using(
 id=(select auth.uid()) or (family_id=(select cuna_private.family()) and (select cuna_private.is_admin()))
);

-- Un alta desde la API administrativa crea la membresía en la MISMA transacción de Auth.
-- raw_app_meta_data sólo la escribe un servidor privilegiado. El alta manual inicial no lleva cuna_creator.
create function cuna_private.attach_member() returns trigger language plpgsql security definer set search_path='' as $$
declare family uuid; assigned_role text; allowed boolean;
begin
 if new.raw_app_meta_data->>'cuna_creator' is null then return new; end if;
 select family_id into strict family from public.members
  where id=(new.raw_app_meta_data->>'cuna_creator')::uuid and role='admin' for share;
 assigned_role := new.raw_app_meta_data->>'cuna_role';
 allowed := (new.raw_app_meta_data->>'cuna_operate')::boolean;
 insert into public.members values(new.id,family,new.email,assigned_role,assigned_role='admin' or allowed);
 update public.families set version=version+1 where id=family;
 return new;
end; $$;
revoke all on function cuna_private.attach_member() from public, anon, authenticated;
create trigger cuna_attach_member after insert on auth.users for each row execute function cuna_private.attach_member();

-- API de mutaciones: el JWT determina actor y familia. La UI nunca envía el family_id.
create function public.cuna_change(action text,p jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
declare actor public.members; current_assignment public.assignments; target uuid;
 device text := p->>'device_id'; boundary timestamptz; new_id uuid; result jsonb := '{}'::jsonb;
begin
 select * into actor from public.members where id=auth.uid() for share;
 if actor.id is null or (action='operate' and not actor.can_operate) or (action<>'operate' and actor.role<>'admin')
 then raise sqlstate '42501' using message='Permiso insuficiente.'; end if;
 case action
 when 'child' then
  insert into public.children(family_id,name) values(actor.family_id,p->>'name') returning id into new_id;
  result:=jsonb_build_object('id',new_id);
 when 'rename' then
  update public.children set name=p->>'name' where family_id=actor.family_id and id=(p->>'child_id')::uuid;
  if not found then raise exception 'Perfil no disponible.'; end if;
 when 'permission' then
  update public.members set can_operate=(p->>'enabled')::boolean
   where family_id=actor.family_id and id=(p->>'member_id')::uuid and role='caregiver';
  if not found then raise exception 'Cuidador no disponible.'; end if;
 when 'device' then
  insert into public.devices values(device,actor.family_id,p->>'name');
  insert into cuna_private.device_keys values(device,encode(sha256(convert_to(p->>'secret','UTF8')),'hex'));
 when 'assign','operate' then
  perform 1 from public.devices where family_id=actor.family_id and id=device for update;
  if not found then raise exception 'Dispositivo no disponible.'; end if;
  select * into current_assignment from public.assignments where device_id=device and ended_at is null;
  if not (p ? 'expected') or current_assignment.id is distinct from (p->>'expected')::uuid
   then raise sqlstate 'PT409' using message='La asignación cambió. Actualiza el panel.'; end if;
  if action='operate' then
   if current_assignment.id is null then raise exception 'Asigna primero el dispositivo.'; end if;
   update public.assignments set enabled=(p->>'enabled')::boolean where id=current_assignment.id;
  else
   target:=(p->>'child_id')::uuid;
   if target is not null and not exists(select 1 from public.children where id=target and family_id=actor.family_id)
    then raise exception 'Perfil no disponible.'; end if;
   if current_assignment.child_id is distinct from target then
    boundary:=clock_timestamp();
    update public.assignments set ended_at=boundary where id=current_assignment.id;
    if target is not null then
     insert into public.assignments(family_id,device_id,child_id,started_at) values(actor.family_id,device,target,boundary);
    end if;
   end if;
  end if;
 else raise exception 'Operación desconocida.';
 end case;
 update public.families set version=version+1 where id=actor.family_id;
 return result;
end; $$;
revoke all on function public.cuna_change(text,jsonb) from public,anon,authenticated;
grant execute on function public.cuna_change(text,jsonb) to authenticated;

create function public.cuna_ingest(device text,secret text,event_id uuid,at_time timestamptz,
 metric_name text,measurement double precision,measurement_unit text) returns jsonb
language plpgsql security definer set search_path='' as $$
declare family uuid; child uuid; prior public.events;
begin
 select d.family_id into family from public.devices d join cuna_private.device_keys k on k.device_id=d.id
  where d.id=device and k.digest=encode(sha256(convert_to(secret,'UTF8')),'hex') for update of d;
 if family is null then raise sqlstate '42501' using message='Credencial de dispositivo inválida.'; end if;
 if at_time > clock_timestamp() then raise exception 'La captura está en el futuro.'; end if;
 select * into prior from public.events where id=event_id;
 if found then
  if (prior.device_id,prior.captured_at,prior.metric,prior.value,prior.unit)
    is distinct from (device,at_time,metric_name,measurement,measurement_unit)
   then raise sqlstate 'PT409' using message='El identificador del evento ya tiene otro contenido.'; end if;
  return jsonb_build_object('child_id',prior.child_id);
 end if;
 select child_id into child from public.assignments where device_id=device and started_at<=at_time
  and (ended_at is null or at_time<ended_at);
 if child is null then raise sqlstate 'PT409' using message='El dispositivo estaba sin asignar en esa fecha.'; end if;
 insert into public.events values(event_id,family,device,child,at_time,metric_name,measurement,measurement_unit);
 update public.families set version=version+1 where id=family;
 return jsonb_build_object('child_id',child);
end; $$;
revoke all on function public.cuna_ingest(text,text,uuid,timestamptz,text,double precision,text) from public,anon,authenticated;
grant execute on function public.cuna_ingest(text,text,uuid,timestamptz,text,double precision,text) to anon,authenticated;
do $$ begin
 if not exists(select 1 from pg_publication_tables where pubname='supabase_realtime' and schemaname='public' and tablename='families') then
  alter publication supabase_realtime add table public.families;
 end if;
end $$;
-- Lectura coherente de una ficha en un único snapshot, con las RLS del solicitante.
create function public.cuna_state(selected_child uuid default null) returns jsonb
language plpgsql stable security invoker set search_path='' as $$
declare actor public.members; selected uuid := selected_child;
begin
 select * into actor from public.members where id=auth.uid();
 if not found then raise sqlstate '42501' using message='Cuenta sin familia asignada.'; end if;
 if selected is null then select id into selected from public.children order by name,id limit 1;
 elsif not exists(select 1 from public.children where id=selected) then raise exception 'Perfil no disponible.'; end if;
 return jsonb_build_object('me',to_jsonb(actor),'selected',selected,
  'children',coalesce((select jsonb_agg(jsonb_build_object('id',id,'name',name) order by name,id) from public.children),'[]'::jsonb),
  'devices',coalesce((select jsonb_agg(to_jsonb(d) order by d.id) from
   (select v.id,v.name,a.id as assignment_id,a.child_id,coalesce(a.enabled,false) as enabled from public.devices v
    left join public.assignments a on a.device_id=v.id and a.ended_at is null) d),'[]'::jsonb),
  'events',coalesce((select jsonb_agg(to_jsonb(e) order by e.captured_at desc,e.id desc) from
   (select id,child_id,device_id,captured_at,metric,value,unit from public.events where child_id=selected
    order by captured_at desc,id desc limit 100) e),'[]'::jsonb),
  'members',case when actor.role='admin' then (select jsonb_agg(to_jsonb(m) order by m.email) from public.members m)
    else '[]'::jsonb end);
end; $$;
revoke all on function public.cuna_state(uuid) from public,anon,authenticated;
grant execute on function public.cuna_state(uuid) to authenticated;
commit;

