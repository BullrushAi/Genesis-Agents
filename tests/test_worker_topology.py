from runtime.worker_topology import worker_topology


def test_in_process_worker_owns_queue_without_separate_render_worker():
    result = worker_topology(
        {
            "GENESIS_WORKER_ENABLED": "true",
            "WORKER_MODE": "trigger_dev",
            "TRIGGER_SECRET_KEY": "",
        }
    )
    assert result["worker_mode"] == "in_process"
    assert result["separate_worker_required"] is False
    assert result["worker_intent"] == "web_service_owns_queue_processing"


def test_trigger_dev_mode_requires_a_configured_trigger_secret():
    missing = worker_topology(
        {
            "GENESIS_WORKER_ENABLED": "false",
            "WORKER_MODE": "trigger_dev",
            "TRIGGER_SECRET_KEY": "",
        }
    )
    assert missing["separate_worker_required"] is True
    assert missing["worker_intent"] == "trigger_dev_missing_secret"

    configured = worker_topology(
        {
            "GENESIS_WORKER_ENABLED": "false",
            "WORKER_MODE": "trigger_dev",
            "TRIGGER_SECRET_KEY": "configured",
        }
    )
    assert configured["worker_mode"] == "trigger_dev"
    assert configured["separate_worker_required"] is False
    assert configured["worker_intent"] == "trigger_dev_owns_job_dispatch"


def test_external_worker_mode_fails_closed_until_a_separate_worker_exists():
    result = worker_topology(
        {
            "GENESIS_WORKER_ENABLED": "false",
            "WORKER_MODE": "external_worker",
            "TRIGGER_SECRET_KEY": "",
        }
    )
    assert result["worker_mode"] == "external_worker"
    assert result["separate_worker_required"] is True
    assert result["worker_intent"] == "separate_worker_required"
