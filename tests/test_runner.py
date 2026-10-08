"""流水线测试：缓存命中、singleflight 防击穿、失败如实落库。"""

from __future__ import annotations

import asyncio

import pytest

from reviewbot.github_source import GitHubFetcher
from reviewbot.runner import ReviewPipeline
from reviewbot.store import ReviewStore
from tests.conftest import make_llm

CODE = "def f(n):\n    for i in range(n):\n        print(i)\n"
SAME_CODE_DIFFERENT_FORMAT = "# 加一行注释\ndef f(n):\n\n    for i in range(n):  # 行尾\n        print(i)\n"


@pytest.fixture
def pipeline(settings, cache, tmp_path):
    store = ReviewStore(str(tmp_path / "pipeline.db"), max_code_chars=500)
    llm, _transport = make_llm()
    github = GitHubFetcher(allowed_hosts=("github.com",), transport=None)
    pipe = ReviewPipeline(settings=settings, store=store, cache=cache, llm=llm, github=github)
    yield pipe
    store.close()


async def test_first_run_succeeds_and_persists(pipeline):
    task_id, source_type, cache_hit = pipeline.submit(code=CODE, github_url=None, language="python")
    assert source_type == "code"
    assert cache_hit is False

    result = await pipeline.run(task_id=task_id, code=CODE, language="python")
    assert result.cache_hit is False
    assert result.llm.degraded is False
    assert result.analysis["structure"]["max_loop_nesting"] == 1

    stored = pipeline.store.get(task_id)
    assert stored is not None and stored.state == "SUCCESS"
    assert stored.result["analysis"]["structure"]["loop_count"] == 1
    assert pipeline.store.stats()["total_reviews"] == 1


async def test_semantically_identical_code_hits_cache(pipeline):
    """核心回归：只加注释与空行，必须命中缓存（v1 的 strip() 做不到）。"""
    first_id, _, _ = pipeline.submit(code=CODE, github_url=None, language="python")
    await pipeline.run(task_id=first_id, code=CODE, language="python")

    second_id, _, cache_hit = pipeline.submit(
        code=SAME_CODE_DIFFERENT_FORMAT, github_url=None, language="python"
    )
    assert cache_hit is True

    stored = pipeline.store.get(second_id)
    assert stored is not None and stored.state == "SUCCESS"
    assert stored.result["cache_hit"] is True
    assert pipeline.store.stats()["cache_hits"] == 1


async def test_cache_key_depends_on_model(settings, cache, tmp_path, monkeypatch):
    """模型不同则不能复用缓存，否则用户会拿到别的模型给出的结论。"""
    store = ReviewStore(str(tmp_path / "m.db"))
    llm_a, _ = make_llm()
    github = GitHubFetcher(allowed_hosts=("github.com",), transport=None)
    pipe = ReviewPipeline(settings=settings, store=store, cache=cache, llm=llm_a, github=github)

    task_id, _, _ = pipe.submit(code=CODE, github_url=None, language="python")
    await pipe.run(task_id=task_id, code=CODE, language="python")

    llm_b, _ = make_llm()
    llm_b.model = "another-model"
    pipe_b = ReviewPipeline(settings=settings, store=store, cache=cache, llm=llm_b, github=github)
    _, _, cache_hit = pipe_b.submit(code=CODE, github_url=None, language="python")
    assert cache_hit is False
    store.close()


async def test_syntax_error_is_not_task_failure(pipeline):
    """语法错误 = 分析成功但代码有问题，任务本身不该标记为 FAILED。"""
    bad = "def f(:\n"
    task_id, _, _ = pipeline.submit(code=bad, github_url=None, language="python")
    result = await pipeline.run(task_id=task_id, code=bad, language="python")

    assert result.analysis["parse_error"] is not None
    assert result.llm.degraded is True  # 跳过 LLM，明确标注降级
    stored = pipeline.store.get(task_id)
    assert stored is not None and stored.state == "SUCCESS"
    assert pipeline.store.stats()["failed_reviews"] == 0
    assert pipeline.store.stats()["llm_degraded"] == 1


async def test_infrastructure_failure_marks_task_failed(pipeline, monkeypatch):
    """LLM 抛异常（而非降级）时，任务必须落库为 FAILED，不能伪装成功。"""

    async def boom(*_args, **_kwargs):
        raise RuntimeError("上游彻底不可用")

    monkeypatch.setattr(pipeline.llm, "review", boom)
    task_id, _, _ = pipeline.submit(code=CODE, github_url=None, language="python")

    with pytest.raises(RuntimeError):
        await pipeline.run(task_id=task_id, code=CODE, language="python")

    stored = pipeline.store.get(task_id)
    assert stored is not None and stored.state == "FAILED"
    assert "上游彻底不可用" in (stored.error or "")
    assert pipeline.store.stats()["failed_reviews"] == 1


async def test_degraded_llm_recorded_separately(pipeline):
    """LLM 降级要能被单独统计出来，而不是混进成功率。"""

    async def degraded(*_args, **_kwargs):
        from reviewbot.llm import LLMReview

        return LLMReview(summary="降级", degraded=True, error="timeout")

    pipeline.llm.review = degraded  # type: ignore[method-assign]
    task_id, _, _ = pipeline.submit(code=CODE, github_url=None, language="python")
    result = await pipeline.run(task_id=task_id, code=CODE, language="python")

    assert result.llm.degraded is True
    stats = pipeline.store.stats()
    assert stats["llm_degraded"] == 1
    assert stats["failed_reviews"] == 0


async def test_github_source_uses_fetcher(pipeline, monkeypatch):
    async def fake_fetch(url: str) -> str:
        assert url.endswith("c.py")
        return CODE

    monkeypatch.setattr(pipeline.github, "fetch", fake_fetch)
    monkeypatch.setattr("reviewbot.github_source._reject_private_address", lambda host: None)

    task_id, source_type, _ = pipeline.submit(
        code=None, github_url="https://github.com/a/b/blob/main/c.py", language="python"
    )
    assert source_type == "github"
    assert pipeline.store.get(task_id) is not None

    result = await pipeline.run(
        task_id=task_id, github_url="https://github.com/a/b/blob/main/c.py", language="python"
    )
    assert result.analysis["structure"]["loop_count"] == 1


async def test_github_failure_marks_failed(pipeline, monkeypatch):
    async def fake_fetch(url: str) -> str:
        raise RuntimeError("网络不可达")

    monkeypatch.setattr(pipeline.github, "fetch", fake_fetch)
    monkeypatch.setattr("reviewbot.github_source._reject_private_address", lambda host: None)
    url = "https://github.com/a/b/blob/main/c.py"
    task_id, _, _ = pipeline.submit(code=None, github_url=url, language="python")

    with pytest.raises(RuntimeError):
        await pipeline.run(task_id=task_id, github_url=url, language="python")

    stored = pipeline.store.get(task_id)
    assert stored is not None and stored.state == "FAILED"


async def test_progress_callback_reports_monotonic_steps(pipeline):
    seen: list[tuple[int, str]] = []

    async def on_progress(progress: int, step: str) -> None:
        seen.append((progress, step))

    task_id, _, _ = pipeline.submit(code=CODE, github_url=None, language="python")
    await pipeline.run(task_id=task_id, code=CODE, language="python", on_progress=on_progress)

    progresses = [p for p, _ in seen]
    assert progresses == sorted(progresses)
    assert progresses[-1] == 100
    assert any(step == "调用大模型分析" for _, step in seen)


async def test_singleflight_waiter_uses_lock_holder_result(pipeline, settings):
    """两个相同请求并发：后者应等前者写入缓存后直接命中，而不是再调一次 LLM。"""
    calls = {"n": 0}
    original = pipeline.llm.review

    async def counting_review(*args, **kwargs):
        calls["n"] += 1
        await asyncio.sleep(0.3)  # 模拟 LLM 耗时，制造并发窗口
        return await original(*args, **kwargs)

    pipeline.llm.review = counting_review  # type: ignore[method-assign]
    settings.singleflight_wait_seconds = 2.0

    id_a, _, _ = pipeline.submit(code=CODE, github_url=None, language="python")
    id_b, _, _ = pipeline.submit(code=SAME_CODE_DIFFERENT_FORMAT, github_url=None, language="python")

    await asyncio.gather(
        pipeline.run(task_id=id_a, code=CODE, language="python"),
        pipeline.run(task_id=id_b, code=SAME_CODE_DIFFERENT_FORMAT, language="python"),
    )

    # 关键断言：LLM 只被调用一次，第二个请求走了 singleflight 等待
    assert calls["n"] == 1
    assert pipeline.store.get(id_b).result["cache_hit"] is True


async def test_repeated_runs_reuse_cache(pipeline):
    calls = {"n": 0}
    original = pipeline.llm.review

    async def counting_review(*args, **kwargs):
        calls["n"] += 1
        return await original(*args, **kwargs)

    pipeline.llm.review = counting_review  # type: ignore[method-assign]

    for _ in range(3):
        task_id, _, _ = pipeline.submit(code=CODE, github_url=None, language="python")
        await pipeline.run(task_id=task_id, code=CODE, language="python")

    assert calls["n"] == 1
    assert pipeline.store.stats()["cache_hits"] == 2
