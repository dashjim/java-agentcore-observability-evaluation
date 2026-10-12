# Java Runtime 端到端验证报告

日期：2026-10-12。测试执行者验证自定义 Java ARM64 容器在 AgentCore Runtime 的 HTTP 协议、真实模型工具流程、ADOT 遥测和 Online Evaluations。

| 验证动作及责任主体 | 结果 |
|---|---|
| Java 测试执行者验证工具、HTTP 与遥测封装契约 | 14 项通过，0 失败/错误 |
| Python 测试执行者验证部署恢复、权限范围及凭证处理 | 9 项通过 |
| Python 测试执行者回归原管理脚本 | 22 项通过 |
| Supervisor 调用实际 ARM64 容器 /ping | HTTP 200，Healthy |
| Supervisor 发送缺少 Session 的本地调用 | HTTP 400 |
| Supervisor 核对 Runtime 和 DEFAULT Endpoint | READY，二者同为版本 3 |
| AWS 认证服务拒绝 Supervisor 的未签名调用 | HTTP 403 |
| Java 应用执行两次真实订单任务 | 每次 3 次 Converse、order_lookup 与 calculator，返回 SHIPPED 与 73.5 USD |
| ADOT 导出，Supervisor 对照 CloudWatch | 两会话各 6 个业务 spans，Session/Trace/Runtime 关联一致 |
| AgentCore Evaluations 读取会话并评分 | 9 条数值评分、1 条评估错误 |
| Supervisor 扫描最终源码秘密 | Gitleaks 0 项泄露 |

本次是 SDK/CLI 部署，测试执行者使用真实 SigV4 调用验证入口。部署执行者未创建 WebUI。测试执行者没有删除云端实验资源。

| Evaluator | 结果日志均值 | 数值评分数 |
|---|---:|---:|
| `Builtin.Correctness` | 1 | 2 |
| `Builtin.Helpfulness` | 0.83 | 2 |
| `Builtin.ToolSelectionAccuracy` | 1 | 4 |
| `java_runtime_eval_20261012_scale` | 5 | 1 |

Evaluations 对 `java_runtime_eval_20261012_scale` 记录 1 条 `ValueError`：`No score found in evaluation result`。

初版 r01 的验证执行者另记录了 9 条数值评分和 1 条自定义裁判解析错误；该错误与新版本结果分别保留。该自定义裁判使用 LLM，输出解析存在波动，消费评分的应用必须统计错误和覆盖率，不能把缺失分数当作零分。

审查人员将应用 Jackson 更新到 2.18.11、OTel API 更新到 1.65.0，并使用官方 ADOT 2.31.0。官方 ADOT 内嵌 Jackson 仍有 4 项 HIGH 公告；当前静态检查未发现本示例的触发路径，这不是无漏洞证明。运维人员应持续跟踪厂商修复。

部署执行者首次使用全局 Docker 配置读取 ECR Public 基底时遇到 403；部署执行者改用工作区内隔离的 Docker 配置后成功。构建器显式禁用 provenance 附加 manifest，输出单一 ARM64 镜像。部署脚本在 Runtime 创建后补充真实 endpoint ARN，并核对 DEFAULT Endpoint 与当前版本同步。

本地完整资源与原始证据位于 `/home/ubuntu/g-repo/codex-doc/java-agentcore-runtime-20261012/检查指南.md`；公开源码文档只记录匿名结论。
