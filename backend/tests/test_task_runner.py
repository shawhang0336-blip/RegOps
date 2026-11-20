"""有界后台任务执行器（TaskRunner）单元测试：去重、异常释放与内联执行模式。"""

import threading

from app.infra.task_runner import TaskRunner


def test_task_runner_deduplicates_in_flight_key_and_releases_after_finish():
    runner = TaskRunner(max_workers=2)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    calls = []

    def work():
        calls.append(1)
        started.set()
        release.wait(timeout=5)
        finished.set()

    try:
        assert runner.submit("run:r1", work) is True
        assert started.wait(timeout=5)
        assert runner.is_active("run:r1") is True
        # 同一任务键运行期间重复提交必须被去重拒绝
        assert runner.submit("run:r1", work) is False

        release.set()
        assert finished.wait(timeout=5)
        runner.shutdown(wait=True)

        assert calls == [1]
        assert runner.is_active("run:r1") is False
    finally:
        release.set()
        runner.shutdown(wait=True)


def test_task_runner_releases_key_even_when_task_raises():
    runner = TaskRunner(inline=True)

    def boom():
        raise RuntimeError("boom")

    assert runner.submit("doc:extract:d1", boom) is True
    assert runner.is_active("doc:extract:d1") is False
    # 失败释放后允许再次重试提交
    ran = []
    assert runner.submit("doc:extract:d1", lambda: ran.append("ok")) is True
    assert ran == ["ok"]
