"""Celery 任务定义。

注意：任务里**没有任何业务逻辑**，只是把参数转交给同一个
:class:`reviewbot.runner.ReviewPipeline`。这样在线程池执行与 Celery 执行
两条路径下，行为与写库结果完全一致，不存在"两套实现慢慢漂移"的问题。
"""

from __future__ import annotations

from reviewbot.celery_app import celery_app, run_async

if celery_app is not None:  # pragma: no cover - 需要 broker 配置才会注册

    @celery_app.task(bind=True, name="reviewbot.analyze", max_retries=0)
    def analyze_task(self, task_id: str, code: str | None, github_url: str | None, language: str) -> dict:
        from reviewbot.bootstrap import build_pipeline

        pipeline = build_pipeline()

        async def _run():
            return await pipeline.run(
                task_id=task_id,
                code=code,
                github_url=github_url,
                language=language,
            )

        result = run_async(_run())
        return result.to_dict()
