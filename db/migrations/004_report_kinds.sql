-- More report kinds; company-payee invoices; per-week email template.
alter table report_versions drop constraint report_versions_kind_check;
alter table report_versions add constraint report_versions_kind_check check (kind in (
  'agent_pdf','combined_pdf','reports_zip','master_xlsx','package_zip','payable_csv','payable_only_csv','paychex_csv',
  'aeg_workbook','aeg_invoice_pdf','aeg_invoice_csv','leaderboard_png','leaderboard_pdf','leaderboard_csv','map_csv'));
alter table report_versions add column label text;            -- e.g. "Final", "Preliminary", "Final Rev 3"
alter table report_versions add column replaced_by uuid references report_versions(id);

create table settings (
  key text primary key,
  value jsonb not null,
  updated_by text not null,
  updated_at timestamptz not null default now()
);
