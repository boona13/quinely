def test_bootstrap_growth_cron_updates_stale_schedule_without_duplicates(tmp_path):
    from ghost_autonomy import DEFAULT_GROWTH_SCHEDULES, GROWTH_JOB_PREFIX, bootstrap_growth_cron
    from ghost_cron import CronService, make_job

    cron_path = tmp_path / "jobs.json"
    cron = CronService(store_path=cron_path, on_fire=lambda job: None)
    stale_job = make_job(
        name=f"{GROWTH_JOB_PREFIX}health_check",
        schedule={"kind": "cron", "expr": "0 */2 * * *"},
        payload={"type": "task", "prompt": "stale prompt"},
        description="stale health check",
        enabled=True,
    )
    cron.store.add(stale_job)

    bootstrap_growth_cron(cron, {"enable_growth": True})

    jobs = cron.store.get_all()
    health_jobs = [job for job in jobs if job["name"] == f"{GROWTH_JOB_PREFIX}health_check"]
    assert len(health_jobs) == 1
    assert health_jobs[0]["schedule"] == {
        "kind": "cron",
        "expr": DEFAULT_GROWTH_SCHEDULES["health_check"],
    }
    assert health_jobs[0]["state"]["nextRunAtMs"] is not None
    assert health_jobs[0]["payload"]["type"] == "task"
    assert health_jobs[0]["payload"]["prompt"] != "stale prompt"

    cron.stop()
