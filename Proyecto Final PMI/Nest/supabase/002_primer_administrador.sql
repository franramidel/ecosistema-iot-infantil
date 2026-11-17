-- Primero crear y confirmar el usuario en Authentication > Users del proyecto.
-- Sustituir el correo de ejemplo antes de ejecutar en el editor SQL.
do $$
declare
 correo text := 'CAMBIAR@example.test';
 usuario uuid;
 familia uuid;
begin
 select id into strict usuario from auth.users where lower(email)=lower(correo) for update;
 if exists(select 1 from public.members where id=usuario) then
  raise notice 'El usuario ya pertenece a una familia.';
  return;
 end if;
 insert into public.families default values returning id into familia;
 insert into public.members(id,family_id,email,role,can_operate)
 values(usuario,familia,lower(correo),'admin',true);
end;
$$;

