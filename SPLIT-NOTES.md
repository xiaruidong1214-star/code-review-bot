# 拆分说明：这个仓库从哪里来

## 一句话

本仓库是 `code-review-bot`（代码审查服务）从**一个混装仓库**中独立出来的结果。

## 背景

我原先把两套完全无关的系统放在同一个仓库里：

- `app/` 目录 —— FastAPI + Celery + Redis + SQLite 的**代码审查系统**
- 根目录的 `tracker.py` / `tasks.py` / `report.py` / `config.py` —— **LLM 成本治理**埋点

它们**共用同一个仓库，却各自使用互不相干的 Redis 配置**（一个走 `settings.REDIS_URL`，另一个手搓 `REDIS_HOST`），
依赖清单里只有 Celery/Redis/OpenAI 三行，根目录的 `mock_llm.py` 还是成本治理那套的演示物料。
更糟的是，同一份代码被推到了两个仓库（`try` 与 `llm-tracker-current`），
导致简历里读起来像"两个项目"，实际上只是一个项目推了两遍。

这种混装带来的真实代价：

1. **依赖互相污染** —— 想单独跑代码审查，必须连带装上成本治理的配置；
2. **README 无法诚实描述** —— 一个仓库要讲两个不相关的架构；
3. **面试时说不清** —— 被问"这两个项目什么关系"时只能承认是同一份代码。

## 现在怎么分

| 原仓库中的东西 | 现在归属 |
|---|---|
| `app/`（FastAPI、Celery、AST 分析、缓存、SQLite、GitHub 抓取） | **本仓库** `code-review-bot` |
| `tracker.py` / `tasks.py` / `report.py` / `config.py` / `mock_llm.py` | [`llm-cost-governor`](https://github.com/xiaruidong1214-star/llm-cost-governor) |

两个仓库都是**独立可运行**的：各自有自己的 `pyproject.toml`、依赖、配置、测试、CI 与 Dockerfile，
不存在跨仓库依赖。

## 拆分时顺带修掉的真实缺陷

重构不是搬文件。原实现在这个子系统里存在以下问题，均已在 v2 修复并补了回归测试：

| # | 原实现的问题 | 现在的做法 | 对应测试 |
|---|---|---|---|
| 1 | 缓存 key 是 `sha256(language + code.strip())`，加一行注释就未命中，"命中率 95%"在语义上站不住 | key = 语言 + **AST 归一化指纹** + 分析器版本 + 模型名；注释、空白、CRLF 不影响命中 | `test_analyzer.py`、`test_cache.py` |
| 2 | 写了 `acquire_lock` 但**全仓库零调用**，防缓存击穿形同虚设 | 锁进主路径 + **singleflight 等待**，断言"LLM 只被调用一次" | `test_runner.py` |
| 3 | `release_lock` 是裸 `DELETE`，锁 TTL 到期后会**误删他人的锁** | token 校验 + **Lua 原子释放** | `test_cache.py` |
| 4 | 递归检测只比较 `child.func.id == node.name`，**互递归 A→B→A 完全漏掉** | 建调用图 + **Tarjan 强连通分量**，覆盖直接/间接/互递归 | `test_callgraph.py` |
| 5 | 用"循环嵌套层数 → O(n^k)"映射复杂度，反例明显（常量内层循环、并列循环方向都会反） | 只报**结构统计**，并在结果里显式声明"不能推导时间复杂度" | `test_metrics.py` |
| 6 | 注释率用 `line.startswith('#')`，字符串里的 `#` 与文档字符串都会算错 | 改用 `tokenize` 统计，行尾注释与 docstring 分离 | `test_metrics.py` |
| 7 | LLM 失败返回 `"分析暂不可用"` 却仍被记为 SUCCESS，"成功率"指标失真 | `llm.degraded` 独立计数；基础设施故障标记 **FAILED**；语法错误单独区分 | `test_runner.py`、`test_api.py` |
| 8 | 用户代码直接插进 prompt，存在**提示注入** | 定界符包裹 + system 消息声明"内部是不可信数据" + JSON schema 校验 | `test_llm.py` |
| 9 | GitHub 抓取是占位符 `"# GitHub 代码获取待实现"`，对外却返回 `source_type="github"` | 真的走 Contents API 抓取，并补 **SSRF 防护**（白名单 + 解析 IP 拒绝内网） | `test_github.py` |
| 10 | `/review/stream` 注释写"流式读取"，代码却是 `await request.body()` 一次读进内存 | `async for chunk in request.stream()`，边收边判上限 | `test_api.py` |
| 11 | SQLite 每次新建连接、全文代码无上限存两遍 | 线程本地连接复用 + WAL + 只存截断预览 | `test_api.py` |

## 旧仓库

原来的混装仓库已重命名并归档保留，作为这段历史的凭证，不再更新：

- `xiaruidong1214-star/legacy-archive-mixed-repo`（原 `try`）
- `xiaruidong1214-star/legacy-archive-llm-tracker`（原 `llm-tracker-current`）

## 参考

- 详细架构与设计取舍见 [README.md](README.md)
- 已知限制见 [README.md 的「已知限制」小节](README.md#已知限制)
