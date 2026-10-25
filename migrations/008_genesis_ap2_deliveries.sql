-- Genesis AP2 Deliveries Tracking
--
-- Gap G3 fix: track delivered negotiations to prevent duplicate delivery
-- after service restarts. An in-memory set is not restart-safe; this table
-- persists delivery state across restarts and deploys.
--
-- negotiation_id is the unique AP2 negotiation UUID from SwarmSync.
-- delivered_at records when /ap2/deliver was successfully called.
-- delivery_id is the optional ID returned by the SwarmSync API.
-- content_hash is the SHA256 of the delivered output for verification.

BEGIN;

CREATE TABLE IF NOT EXISTS genesis_ap2_deliveries (
  negotiation_id text PRIMARY KEY,
  delivered_at   timestamptz NOT NULL DEFAULT now(),
  delivery_id    text,
  content_hash   text,
  responder_agent_id text,
  is_success     boolean NOT NULL DEFAULT true,
  failure_reason text
);

CREATE INDEX IF NOT EXISTS idx_ap2_deliveries_delivered_at 
  ON genesis_ap2_deliveries(delivered_at DESC);

CREATE INDEX IF NOT EXISTS idx_ap2_deliveries_responder
  ON genesis_ap2_deliveries(responder_agent_id);

COMMIT;
