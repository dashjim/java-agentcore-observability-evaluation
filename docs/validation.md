# Java ADOT 与 AgentCore Evaluation 实测

实验日期：2026-09-16，AWS Region 为 us-east-1。实验执行者在 EC2 主机上启动 Java 应用；被测系统是 Java 业务埋点、ADOT Java 直接导出、X-Ray／CloudWatch 接收和 AgentCore Evaluations 自动评分组成的链路。

## 实验目标与方法

实验执行者需要回答客户的三个问题：第三方 Java Agent 能否接入，Online 能否提供汇总，以及自定义指标是否限制在 0–1。Java 应用使用 instance role 调用真实 Bedrock Nova Lite，不使用模拟模型响应。

Java 应用执行订单任务，Bedrock 业务模型先请求 order_lookup，再请求 calculator，最后根据工具返回的 SHIPPED、24.50 USD 和数量 3 生成 73.5 USD 的回答。Java 应用负责实际工具执行和 span 生命周期；ADOT 负责共享 provider、SigV4 和直接导出；AgentCore Evaluations 负责发现会话与调用裁判。实验执行者没有部署 ADOT Collector，也没有创建 AgentCore Runtime。

每个完成的会话包含 6 个手工业务 spans：1 个 invoke_agent、3 个 chat、2 个 execute_tool。同一业务 trace 另有 3 个 SDK 自动 spans。进程还可能产生与业务 trace 无关的自动遥测，因此分析人员不能把日志组总事件数直接当成业务 span 数。

## 已核实的链路与评分语义

实验执行者在 CloudWatch 来源日志中核实 scope、session.id、resource service.name 和 aws.service.type=gen_ai_agent，以及真实模型和工具内容。AgentCore 的实际发现查询包含 aws.service.type=gen_ai_agent；只写入 span 属性不能替代 resource 属性。

AgentCore Online 写出的 Helpfulness、Correctness 和 ToolSelectionAccuracy 是独立指标。CloudWatch 在相同 service/config 维度下发布 Average、SampleCount、Sum、Minimum 和 Maximum。数值型 custom judge 保留 1／3／5 量表，按需 API 已返回 5.0。CloudWatch 的 Sum 是单一指标的累计值，不是四项指标的业务总分。

首次历史验证的两个会话产生 10 条数值评分：Helpfulness 均为 0.83，Correctness 均为 1.0，ToolSelectionAccuracy 的 4 个工具目标均为 1.0，custom 的两项均为 5.0。仓库的匿名结果样例来自这次历史记录；真实资源 ID 和原始文件不随源码发布。

## 本次保留复跑中的异常

实验执行者重新创建资源并保留日志。前两个复跑会话产生 8 条内置数值评分和 2 条 custom 错误事件，错误为 No score found in evaluation result。它不是零分。第三次 Java 运行使用安全审查后的依赖，已成功执行 3 次真实模型调用和 2 次工具调用；Online 随后给该会话写出 5 条数值评分，其中原 custom 裁判返回 5.0。该组最终共有 15 条结果事件：13 条数值评分、2 条错误。

实验执行者复核相同输入时观察到原裁判有时返回 5.0；仅增加 token 上限并没有消除错误。实验执行者进一步明确裁判角色与证据边界，并采用 2048 tokens 上限，按需评价两个复跑会话均返回 5.0。现有证据不足以认定失败根因，也不能保证 judge 对所有输入稳定。分析应用须单独记录错误、覆盖率和缺失值。

AgentCore 锁定被 Online 配置引用的 evaluator 后，会拒绝直接更新该 evaluator。实验执行者保留旧 evaluator，另建版本进行诊断与验证，未删除旧错误记录。

## 发布验证范围

开发者使用 JDK 21、ADOT 2.30.0、OTel API 1.64.0、AWS SDK 2.54.19、Jackson BOM 2.18.9 构建本仓库。Java 工具契约测试通过，包含空格路径的独立构建通过，schema 模式可在不加载 ADOT／不调用模型的情况下输出真实工具定义。

开发者对运维脚本执行本地安全测试和 ShellCheck。实验执行者还用发布脚本执行真实 AWS 配置与 Java 启动，逐项核对来源 spans、Online 结果和 CloudWatch 指标。最终结果表由下文的发布验证记录列明。

此示例验证普通 Java 主程序；开发者没有验证指定 Spring AI／LangChain4j 版本的自动覆盖、异步上下文传播、生产流量、故障恢复压力或裁判质量校准。Online 发现与评分是异步处理，1 分钟 session timeout 不是评分完成 SLA。新配置的首次回看查询曾因查询结束时间早于日志组创建时间而返回 MalformedQueryException；服务随后一次发现查询成功。实验执行者保留审计证据，没有因此删除或重建日志组。


## 最终发布脚本的 AWS 验证结果

实验执行者使用本仓库 setup_evaluation.py 创建独立资源，使用 run.sh 连续启动两个会话，随后使用 collect_evidence.py 读取云端证据。Java 两个会话分别在 06:39:20 UTC 和 06:39:41 UTC 完成；06:50:16 UTC 的采集确认 AgentCore Online 已写出 **10 条数值评分、0 条评估错误**，CloudWatch 指标与日志逐项对账一致。

| AgentCore evaluator | CloudWatch Average | SampleCount | Sum | 目标 |
|---|---:|---:|---:|---|
| Builtin.Helpfulness | 0.83 | 2 | 1.66 | 每会话一个 trace |
| Builtin.Correctness | 1.0 | 2 | 2.0 | 每会话一个 trace |
| Builtin.ToolSelectionAccuracy | 1.0 | 4 | 4.0 | 每会话两个工具 spans |
| Custom 1／3／5 量表 | 5.0 | 2 | 10.0 | 每会话一个 trace |

实验执行者核实两会话各有 6 个手工业务 spans，内容完整，resource service type 为 gen_ai_agent。新配置使用资源级 CloudWatch 日志策略；实际 API 要求仅传 resourceArn，不能同时传 policyName。脚本已修复该问题，并通过同一 state 的实际 --resume 完成资源创建；14 项本地脚本测试包括该 API 参数回归。

实验执行者未清理旧组的 13 条评分和 2 条错误，也未清理本组的 10 条评分。两个 Online 配置保持 ACTIVE／ENABLED，四个来源／结果日志组均为 Never expire，所有相关 evaluator、角色和策略保留至用户明确要求删除。账户内的资源标识、Console 链接与原始证据清单由单独检查指南提供。
