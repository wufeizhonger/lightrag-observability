# 简历项目改造说明：为 LightRAG 增加生产级可观测性

> 本文档面向求职/面试，记录本次二次开发的背景、设计决策、简历写法与高频面试问答。
> 英文技术文档见 [Observability.md](./Observability.md)。

## 一、项目背景

[LightRAG](https://github.com/HKUDS/LightRAG) 是香港大学数据科学团队开源的、基于**知识图谱 + 向量检索**的 RAG 框架，后端使用 **Python + FastAPI**，支持 OpenAI / Ollama 等多种模型后端，具备文档解析、图检索、混合检索等能力。

**改造前的代码调研结论**：上游项目已经具备登录鉴权（JWT/API Key）、登录限流、请求体大小限制、全局异常处理、`/health` 健康检查和流式输出（StreamingResponse）。因此这些“常见脚手架”不适合作为改造点。

**真正的空白**：项目缺少**生产级指标监控（Prometheus）与请求链路追踪（Request-ID）**——这正是后端服务从“能跑”走向“可运维”的关键能力，也是面试中体现工程素养的高频话题。

## 二、改造内容

新增模块 `lightrag/api/observability.py`，并在服务主入口 `lightrag_server.py` 集成：

1. **Prometheus 指标暴露**（`/metrics`）：
   - `lightrag_http_requests_total`：请求计数（Counter，标签 method/route/status）；
   - `lightrag_http_request_duration_seconds`：请求耗时（Histogram，标签 method/route）；
   - `lightrag_http_requests_in_progress`：在途请求数（Gauge，标签 method）。
2. **请求链路追踪**：为每个请求生成或透传 `X-Request-ID`，并通过 `ContextVar` 暴露给业务代码与日志，便于跨服务排查。
3. **优雅降级**：`prometheus_client` 为可选依赖，未安装时请求追踪照常工作，`/metrics` 返回 503 并给出安装提示，而不是让服务崩溃。
4. **工程化**：新增 `.[observability]` 可选依赖分组；新增 13 个独立单元测试（`tests/api/test_observability.py`），无需 LLM/GPU 即可验证。

## 三、关键设计决策（面试重点）

- **低基数（low-cardinality）标签**：`route` 标签使用路由模板（如 `/documents/{doc_id}`）而非原始路径，避免每个资源 id 都产生一个时间序列，导致 Prometheus 基数爆炸；未匹配路由（404）统一归入 `unmatched`。
- **纯 ASGI 中间件 + 包装 `send`**：不使用 `BaseHTTPMiddleware`，而是实现原生 ASGI 中间件，在 `http.response.start` 事件中捕获真实状态码，并在 `finally` 中记录指标；对 `StreamingResponse` 也能正确统计。
- **中间件栈顺序**：观测中间件注册为最外层（Starlette 中后添加的在最外层），从而度量包含 CORS / 请求体限制 / 准入控制在内的完整耗时，并捕获所有状态码；只追加 `X-Request-ID`，不删除内层已设置的 CORS 头。
- **可选依赖与独立注册表**：指标使用独立的 `CollectorRegistry`，避免与默认注册表冲突；测试可注入全新 registry，互不污染。

## 四、验证方式

- 测试在最小依赖（fastapi + httpx + pytest + prometheus_client）下运行，无需 LLM、向量库或外部服务：

```bash
pip install -e ".[observability]"
pytest tests/api/test_observability.py -v
```

- 结果：**13 个用例全部通过**，覆盖指标计数、耗时、在途请求（含慢请求并发场景）、路由模板归一化、404/500、X-Request-ID 生成与透传、ContextVar 绑定以及无依赖降级。

## 五、简历写法（可直接参考）

> **基于知识图谱的 RAG 知识库问答系统（LightRAG 二次开发）**　Python / FastAPI / Prometheus
>
> - 基于开源 GraphRAG 框架二次开发，后端采用 FastAPI 异步架构，集成 Ollama 本地大模型，实现文档解析、向量+图谱混合检索与流式问答。
> - **主导设计并落地生产级可观测性模块**：基于纯 ASGI 中间件实现 Prometheus `/metrics` 指标（请求量、耗时分布、在途请求数）与 `X-Request-ID` 全链路追踪，将指标采集置于中间件最外层，覆盖 CORS/限流/准入全链路。
> - 使用**路由模板归一化**解决指标标签高基数问题；通过可选依赖 + 独立注册表实现优雅降级，未安装监控组件时服务功能不受影响。
> - 为模块编写 13 个独立单元测试（TestClient/httpx），无需外部依赖即可验证，测试全部通过；使用 Docker 容器化部署。

## 六、高频面试问答（自测）

1. **为什么用 Histogram 而不是直接记录平均耗时？**
   平均耗时会掩盖长尾，且无法聚合；Histogram 把观测落入可配置的桶（bucket），可在 Prometheus 侧计算 P50/P95/P99，跨实例也能正确聚合。

2. **什么是高基数标签，为什么危险？**
   标签取值过多（如用户 id、请求 id 直接作为标签）会让时间序列数量爆炸，内存与抓取成本激增。本项目用路由模板替代原始路径，只保留有限枚举维度。

3. **为什么不用 `BaseHTTPMiddleware`？**
   它在流式响应、异常传播上有一些已知边界问题，且多一层缓冲；纯 ASGI 中间件更轻量，能在 `http.response.start` 精确拿到状态码，天然兼容流式响应。

4. **中间件为什么放最外层？**
   最外层能包裹其它中间件，度量用户真正感知到的端到端耗时，并能统计到被内层中间件（如 413/429）短路的请求。

5. **`X-Request-ID` 的作用？**
   把一次请求在网关、应用、日志、下游服务中关联起来，线上故障时可用同一个 id 串起所有日志，快速定位。

6. **可选依赖如何做到不崩溃？**
   顶层用 `try/except` 导入 `prometheus_client`，缺失时设置标志位并让指标采集跳过，`/metrics` 返回 503；核心的请求追踪不依赖该库，因此功能不受影响。

## 七、可继续迭代的方向（加分项）

- 接入 Grafana 看板与告警规则（P99 延迟、5xx 比例、在途请求积压）；
- 在异常处理中间件中复用 `X-Request-ID`，让 5xx 响应体直接返回该 id；
- 增加按业务维度（检索模式、模型后端）的指标与 token 用量统计。
