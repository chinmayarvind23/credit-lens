-- Apply once as an administrator in a dedicated public synthetic demo project.
begin;
create table public.creditlens_demo_borrowers (
    borrower_id text primary key check (borrower_id ~ '^borrower-00[1-5]$'),
    name text not null check (length(name) between 1 and 100),
    industry text not null check (length(industry) between 1 and 100),
    published boolean not null default false
);
alter table public.creditlens_demo_borrowers enable row level security;
alter table public.creditlens_demo_borrowers force row level security;
revoke all on public.creditlens_demo_borrowers from public, anon, authenticated;
grant usage on schema public to anon, authenticated;
grant select (borrower_id, name, industry) on public.creditlens_demo_borrowers to anon, authenticated;
create policy public_fictional_directory on public.creditlens_demo_borrowers
    for select to anon, authenticated using (published);
insert into public.creditlens_demo_borrowers (borrower_id, name, industry, published) values
    ('borrower-001', 'Northstar Fabrication', 'Manufacturing', true),
    ('borrower-002', 'Cedar Freight', 'Healthcare', true),
    ('borrower-003', 'Harbor Medical', 'Logistics', true),
    ('borrower-004', 'Prairie Foods', 'Retail', true),
    ('borrower-005', 'Synthetic Enterprise 005', 'Professional services', true);
commit;
