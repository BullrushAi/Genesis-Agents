# Genesis job status

`GET /agents/jobs/{job_id}` returns the `genesis_jobs` row. Its `status` is the job status. It does not return `COMPLETED`.

Statuses that endpoint can return:

- `QUEUED` — `create_job` inserted the row.
- `RUNNING` — the worker claimed it, or an inline child job was inserted already running.
- `DELIVERED` — the worker finished a successful run and stored the result. This is the success terminal for `genesis_jobs`. The stored `resultSummary` carries this same status.
- `DELIVERED_WITH_ARTIFACT_WARNING` — the run succeeded, but artifact upload failed. The stored `resultSummary` carries this same status.
- `FAILED` — the agent returned not ok, or the worker raised.
- `EXPIRED` — a `RUNNING` job's heartbeat went stale.
- `DISPUTED` — a buyer filed a dispute.
- `SETTLED` — an admin resolved a dispute without a refund.
- `REFUNDED` — an admin refunded the job.

`DELIVERED` means the job reached its success terminal. It is not renamed to `COMPLETED`.

`COMPLETED` is not a `genesis_jobs` status. Session finish and delegation relationship rows may use `COMPLETED`. The job status endpoint does not.
