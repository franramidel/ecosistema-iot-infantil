-- Esquema de la base tal como la dejaba la entrega SCRUM-20-21-Ismael.patch. Sirve para probar la migración.

create table if not exists families(id text primary key, version integer not null default 0);
create table if not exists members(
 id text primary key, family_id text not null references families, email text unique not null,
 password_hash text not null, role text not null check(role in ('admin','caregiver')),
 can_operate integer not null);
create table if not exists children(id text primary key, family_id text not null references families, name text not null);
create table if not exists devices(
 id text primary key, family_id text not null references families, name text not null, secret_hash text not null);
create table if not exists assignments(
 id text primary key, device_id text not null references devices, child_id text not null references children,
 started_at text not null, ended_at text, enabled integer not null default 0,
 check(ended_at is null or ended_at >= started_at));
create unique index if not exists one_active on assignments(device_id) where ended_at is null;
create index if not exists period on assignments(device_id,started_at);
create table if not exists events(
 id text primary key, device_id text not null references devices, child_id text not null references children,
 captured_at text not null, metric text not null, value real not null, unit text not null);
create index if not exists timeline on events(child_id,captured_at,id);
create trigger if not exists keep_event before update on events begin select raise(abort,'Evento inmutable'); end;
create trigger if not exists keep_history before delete on events begin select raise(abort,'Evento inmutable'); end;


create table if not exists audit_log(
 id text primary key default (lower(hex(randomblob(4)))||'-'||lower(hex(randomblob(2)))||'-4'||substr(lower(hex(randomblob(2))),2)||'-'||substr('89ab',abs(random())%4+1,1)||substr(lower(hex(randomblob(2))),2)||'-'||lower(hex(randomblob(6)))),
 family_id text not null references families,
 occurred_at text not null,
 event_date text not null generated always as (date(occurred_at,'-6 hours')) stored,
 event_time text not null generated always as (time(occurred_at,'-6 hours')) stored,
 event_type text not null check(length(event_type) between 1 and 50),
 detail text not null,
 child_id text not null references children,
 device_id text not null references devices,
 severity text not null default 'info' check(severity in ('info','warning','critical')),
 source_event_id text);
create unique index if not exists audit_source on audit_log(source_event_id) where source_event_id is not null;
create index if not exists audit_family_time on audit_log(family_id,occurred_at);
create index if not exists audit_child_time on audit_log(child_id,occurred_at);
create trigger if not exists audit_keep_update before update on audit_log begin select raise(abort,'Registro de auditoría inmutable'); end;
create trigger if not exists audit_keep_delete before delete on audit_log begin select raise(abort,'Registro de auditoría inmutable'); end;

create trigger if not exists audit_reading after insert on events begin
 insert into audit_log(family_id,occurred_at,event_type,detail,child_id,device_id,source_event_id)
 select d.family_id,new.captured_at,'LECTURA',new.metric||': '||new.value||' '||new.unit,new.child_id,new.device_id,new.id
 from devices d where d.id=new.device_id;
end;
create trigger if not exists audit_assigned after insert on assignments begin
 insert into audit_log(family_id,occurred_at,event_type,detail,child_id,device_id,source_event_id)
 select d.family_id,new.started_at,'ASIGNACION','Dispositivo asignado al bebé',new.child_id,new.device_id,new.id||':inicio'
 from devices d where d.id=new.device_id;
end;
create trigger if not exists audit_unassigned after update of ended_at on assignments
 when old.ended_at is null and new.ended_at is not null begin
 insert into audit_log(family_id,occurred_at,event_type,detail,child_id,device_id,source_event_id)
 select d.family_id,new.ended_at,'DESASIGNACION','Dispositivo retirado del bebé',new.child_id,new.device_id,new.id||':fin'
 from devices d where d.id=new.device_id;
end;
create trigger if not exists audit_toggled after update of enabled on assignments when old.enabled<>new.enabled begin
 insert into audit_log(family_id,occurred_at,event_type,detail,child_id,device_id)
 select d.family_id,strftime('%Y-%m-%dT%H:%M:%f000+00:00','now'),
  case when new.enabled then 'ACTIVACION' else 'DESACTIVACION' end,
  case when new.enabled then 'Sensor activado' else 'Sensor desactivado' end,new.child_id,new.device_id
 from devices d where d.id=new.device_id;
end;


create trigger if not exists keep_assignments before delete on assignments begin
 select raise(abort,'Asignación inmutable');
end;
