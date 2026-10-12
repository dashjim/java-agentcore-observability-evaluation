# Java AgentCore Runtime 部署需求

日期：2026-10-12。

## 背景与任务

现有 Java 应用在 EC2 执行订单查询与计算，ADOT Java 向 AWS 发送业务遥测，AgentCore Evaluations 对会话评分。用户要求在 AgentCore Runtime 再部署一份。部署执行者沿用现有账户的 us-east-1，保留原实验与新建资源。

## 实施范围

- 部署执行者使用自定义 Java ARM64 容器部署 AgentCore Runtime（Model B），网络模式 PUBLIC，调用者通过 IAM SigV4 认证。用户已明确选择 Runtime 和现有 Java 项目，因此无需重新选择 Harness 或改写为 Strands。
- Java HTTP 服务提供 GET /ping 和 POST /invocations，Java 应用从 Runtime 请求头读取 Session ID，每次请求独立维护模型调用与工具执行状态。
- Java 应用继续调用 Bedrock Nova Lite，执行 order_lookup 与 calculator，返回订单状态、73.5 USD、Session ID 与 Trace ID。
- ADOT Java 导出完整业务 spans；AgentCore Evaluations 使用独立配置对 Runtime 会话评分；CloudWatch 保存日志和指标。
- 部署脚本记录 Runtime ARN、镜像摘要、角色、Endpoint、会话及证据。部署执行者保留资源和结果，不自动清理。

## 验收标准

部署执行者核实 Runtime 与 DEFAULT Endpoint 为 READY；真实签名调用返回订单回答和两个已执行工具；Java 遥测使用与调用相同的 Session ID；CloudWatch 存在 spans，AgentCore Online Evaluations 产生数值评分。部署执行者执行本地协议测试、秘密扫描及依赖审查，并区分实测通过与尚未完成的检查。

本次范围不含 WebUI、Portal 发布、GitHub 发布或原 EC2 实验变更。调用者通过 SDK/CLI 使用部署结果。

## 依据

- AWS 官方 Runtime service contract：https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-service-contract.html
- AWS 官方 Runtime permissions：https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html
- 工作区原项目的 Java 应用、ADOT 启动器和 setup_evaluation.py。
