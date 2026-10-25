-- Genesis /verify job durability.
--
-- The gateway used to keep /verify and arbitrage verification jobs in a module
-- dict. An OOM kill or deploy lost every pending/running job, GET /verify/{id}
-- returned 404 for a jobId SwarmSync had already stored, and the SwarmSync
-- callback never came. This table is the durable record.
--
-- One row per (kind, environment, verification_id). The unique key is what makes
-- POST /verify idempotent: SwarmSync re-dispatches a stale verification with the
-- SAME verification_id and gets the existing job back instead of a second run.
--
-- status          pending | running | completed | failed       (the run)
-- callback_state  none | callback_pending | delivered | rejected (the SwarmSync POST)
--   callback_pending = a result exists but SwarmSync has not accepted it yet;
--   startup recovery and a re-dispatch of the same verification re-send it.
--   rejected = SwarmSync answered a non-retryable 4xx; never re-sent.
--
-- Additive only. No existing table is touched.

BEGIN;

CREATE TABLE IF NOT EXISTS genesis_verification_jobs (
  job_id            text PRIMARY KEY,
  kind              text NOT NULL CHECK (kind IN ('verify', 'arbitrage')),
  environment       text NOT NULL,
  verification_id   text NOT NULL,
  request           jsonb NOT NULL,
  status            text NOT NULL DEFAULT 'pending',
  result            jsonb,
  error             text,
  callback_state    text NOT NULL DEFAULT 'none',
  callback_body     jsonb,
  callback_attempts integer NOT NULL DEFAULT 0,
  created_at        timestamptz NOT NULL DEFAULT now(),
  completed_at      timestamptz,
  heartbeat_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (kind, environment, verification_id)
);

CREATE INDEX IF NOT EXISTS genesis_verification_jobs_outstanding_idx
  ON genesis_verification_jobs (created_at)
  WHERE status IN ('pending', 'running') OR callback_state = 'callback_pending';

COMMIT;
