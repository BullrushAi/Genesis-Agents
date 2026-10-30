-- Open needs-board bids.
--
-- A bid is the only pointer to a held escrow when the marketplace work-order
-- list route is missing. The previous default, a JSON file under /var/data,
-- disappears on a Render restart unless a persistent disk is mounted. This
-- table lives in the same Postgres database as the other restart-critical
-- Genesis state (jobs, AP2 nonces, audit). One row per open bid. The row is
-- removed only after a real delivery (HTTP < 300) or an already-delivered
-- conflict (HTTP 409). A failed insert refuses the bid; the gateway does not
-- bid and then hope a file write sticks.

BEGIN;

CREATE TABLE IF NOT EXISTS genesis_needs_bids (
  env     text             NOT NULL,
  slug    text             NOT NULL,
  need_id text             NOT NULL,
  bid_at  double precision NOT NULL,
  PRIMARY KEY (env, slug, need_id)
);

COMMIT;
