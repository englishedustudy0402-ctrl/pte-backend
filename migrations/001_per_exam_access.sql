-- Per-exam entitlement: one payment unlocks exactly ONE exam.
-- Run in Supabase SQL editor (or via psql). Idempotent.

-- 1. Per-exam expiry columns on profiles (paid access per exam).
alter table public.profiles
  add column if not exists pte_expires_at   timestamptz null,
  add column if not exists ielts_expires_at timestamptz null,
  add column if not exists det_expires_at   timestamptz null;

-- 2. Which exam a subscription/order was purchased for.
alter table public.subscriptions
  add column if not exists exam text null;

-- Backfill legacy subscriptions: they were all-access 'pro' purchases made
-- before per-exam existed. We let the profiles' own expiry columns govern, so
-- existing orders get tagged 'pte' purely for reporting.
update public.subscriptions
  set exam = 'pte'
  where (exam is null or exam = '');

-- 3. (Optional, recommended) index for status polling per user.
create index if not exists idx_subscriptions_user_order
  on public.subscriptions (user_id, status, created_at desc);