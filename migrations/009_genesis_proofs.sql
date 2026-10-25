-- Genesis proof records and the job -> proof link.
--
-- The problem this fixes
-- ----------------------
-- proof_bridge.py::_persist_proof writes one row per proved job into
-- genesis_proofs and then stamps genesis_jobs."proofId"; main.py::verify_proof
-- reads genesis_proofs back for GET /proofs/{proof_id}/verify. No gateway
-- migration (001-008) ever created that table or that column. It only worked
-- because the earlier database had inherited both from a SwarmSync Prisma
-- migration. On a database this gateway owns end to end, _persist_proof raised
-- "relation genesis_proofs does not exist", was swallowed by its non-fatal
-- except, and every proof silently went unrecorded.
--
-- Shape
-- -----
-- Columns, types and nullability follow what the Python code writes and reads
-- (proof_bridge.py::_persist_proof, main.py::verify_proof), which is also the
-- shape the SwarmSync GenesisProof model used, so existing rows copy across
-- unchanged.
--   * "jobId" is UNIQUE: _persist_proof upserts with ON CONFLICT ("jobId").
--   * No foreign key to genesis_jobs: the proof row is written before the job
--     row is stamped with its id, and a proof must survive any later job-row
--     cleanup; it is a signed record in its own right.
--   * "totalAuditEntries" / "totalToolCalls" are never written by the gateway;
--     they keep their zero defaults so the table stays row-compatible.
--   * genesis_jobs."proofId" is unique, one proof per job.
--
-- Idempotent: safe to re-run on a database that already has either object.
-- Schema-agnostic: nothing here names a schema, so the objects are created in
-- whichever schema the connection's search_path selects (the gateway role pins
-- it), exactly like migrations 001-008.

BEGIN;

CREATE TABLE IF NOT EXISTS genesis_proofs (
  id                     text PRIMARY KEY,
  "jobId"                text UNIQUE,
  "agentSlug"            text NOT NULL,
  "buyerClientId"        text,
  "buyerWalletId"        text,
  "proofBundleUri"       text NOT NULL,
  "proofBundleSignedUrl" text,
  "conduitSessionId"     text NOT NULL,
  "conduitPublicKey"     text NOT NULL,
  "inputHash"            text NOT NULL,
  "outputHash"           text NOT NULL,
  "vcapWrapperJwt"       text NOT NULL,
  "totalAuditEntries"    integer NOT NULL DEFAULT 0,
  "totalToolCalls"       integer NOT NULL DEFAULT 0,
  "durationSeconds"      double precision,
  "createdAt"            timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS genesis_proofs_agent_slug_idx
  ON genesis_proofs ("agentSlug");
CREATE INDEX IF NOT EXISTS genesis_proofs_buyer_client_idx
  ON genesis_proofs ("buyerClientId");
CREATE INDEX IF NOT EXISTS genesis_proofs_created_at_idx
  ON genesis_proofs ("createdAt");

ALTER TABLE genesis_jobs ADD COLUMN IF NOT EXISTS "proofId" text;

CREATE UNIQUE INDEX IF NOT EXISTS genesis_jobs_proof_id_key
  ON genesis_jobs ("proofId");

COMMIT;
