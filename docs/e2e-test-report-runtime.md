# Java Runtime 与遥测封装验证报告

记录日期：2026-10-12 UTC。部署执行者已在 us-east-1 将 Java 应用部署到 AgentCore Runtime；最新 Runtime 和 DEFAULT Endpoint 均为 READY、版本 3。

Java 业务类通过 AgentTelemetry 的 agent/chat/tool 三个入口记录遥测。测试执行者确认 14 项 Java 测试、9 项 Runtime 部署脚本测试通过；既有 Python 管理脚本 22 项测试通过。测试执行者确认生产 JAR 不包含 test-only OTel SDK，原 CLI schema 与遥测字段契约保持一致。

Supervisor 核实新版 Java 应用的两个真实 Session 均成功执行 order_lookup 与 calculator，每个会话调用 Bedrock 3 次，返回订单 ORD-1001、SHIPPED、73.5 USD。ADOT 在每个会话导出 6 个业务 spans，验证执行者逐条核对 Session、Trace 和 Runtime Endpoint 关联。AWS 拒绝未签名调用，HTTP 状态为 403。

初版 r01 的 AgentCore Online Evaluations 已产生 9 条数值评分和 1 条自定义裁判解析错误，错误为 No score found in evaluation result；验证执行者将日志与 CloudWatch 指标逐项对账，计数一致。该错误记录保留。新版 r02 的 Online 评分仍在服务异步处理中，尚不能表述为全部评分通过。

审查人员对原47项 Maven 坐标修复后检查、2项新增测试依赖检查和7项固定Python依赖检查均未匹配已知公告。官方ADOT2.31.0仍含4项HIGH公告，ECR基础镜像扫描另有MEDIUM与UNDEFINED发现，详细边界见[安全审查](security-review-runtime.md)。

部署执行者保留AWS资源和所有成功/失败记录；原始账号状态、会话与遥测只在本地交付中保存，公开源码只记录匿名结论。
