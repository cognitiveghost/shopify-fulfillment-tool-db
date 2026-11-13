insert into storage.buckets (id, name, public) values ('files', 'files', false)
on conflict (id) do nothing;

create policy "files authenticated all" on storage.objects for all to authenticated
  using (bucket_id = 'files' and (select auth.role()) = 'authenticated')
  with check (bucket_id = 'files' and (select auth.role()) = 'authenticated');
