from __future__ import annotations

import asyncio

import tender_monitor.scheduler.runner as runner_module
from tender_monitor.scheduler.runner import Runner


class _FakeScheduler:
    def __init__(self) -> None:
        self.paused = False
        self.shutdown_wait: bool | None = None

    def pause(self) -> None:
        self.paused = True

    def shutdown(self, wait: bool = True) -> None:
        self.shutdown_wait = wait


async def test_runner_stop_drains_active_jobs_before_scheduler_shutdown(
    monkeypatch,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_job(_source_name: str) -> None:
        started.set()
        await release.wait()

    monkeypatch.setattr(runner_module, "_job", fake_job)

    runner = Runner()
    fake_scheduler = _FakeScheduler()
    runner.scheduler = fake_scheduler  # type: ignore[assignment]

    job_task = asyncio.create_task(runner._run_job("goszakup"))
    await started.wait()
    assert len(runner._active_jobs) == 1

    stop_task = asyncio.create_task(runner.stop())
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert fake_scheduler.paused is True
    assert fake_scheduler.shutdown_wait is None

    release.set()
    await stop_task
    await job_task

    assert fake_scheduler.shutdown_wait is False
    assert runner._active_jobs == set()
