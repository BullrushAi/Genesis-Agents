-- Lease ownership for genesis_verification_jobs.
--
-- During a Render rolling restart the OLD container keeps executing an in-flight verification while the NEW
-- container's startup recovery re-ran the same job: two workers, one job. A job is now executed only by the worker
-- that holds its lease.
--
-- owner_id          the gateway process (random per process start) that last claimed the job
-- lease_expires_at  until when that claim is valid. A live owner renews it every ~10s; on graceful shutdown it
--                   is released immediately (set to now()); after a hard crash it simply runs out. There are no
--                   permanent locks: any worker may take a job whose lease has expired or was released.
--
-- Additive and idempotent. Rows created before this migration have NULL in both columns, which means "unowned".

BEGIN;

ALTER TABLE genesis_verification_jobs ADD COLUMN IF NOT EXISTS owner_id text;
ALTER TABLE genesis_verification_jobs ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz;

COMMIT;
