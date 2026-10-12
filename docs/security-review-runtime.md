# Java AgentCore Runtime 依赖与源码安全审查

审查日期：2026-10-12（UTC）。审查人员检查现有 Java Agent 的 Runtime 扩展、实际解析的依赖和官方 ADOT JAR。本报告补充 2026-09-16 的历史报告，不修改历史结论。

**审查结果：应用依赖修复后，OSV 未命中已知公告；官方 ADOT 2.31.0 仍携带 4 项 HIGH 级 Jackson 公告。因此本次结果不能表述为“全部依赖无漏洞”或“安全扫描全部通过”。** 审查人员依据官方 JAR 的静态调用证据，没有发现当前 HTTP 请求可以到达这些公告触发路径；这一判断限定于当前代码、配置和扫描范围，不等同于完整不可达证明。

## 背景与任务

Java 应用负责接收 Runtime 请求、校验参数、调用 Bedrock 模型和执行订单工具；Bedrock 模型负责工具选择与最终回答；ADOT Java 负责共享 OpenTelemetry provider 和遥测传输；AgentCore Runtime 负责托管容器与 IAM 入站鉴权；AgentCore Evaluations 负责读取会话并调用 evaluator。审查人员负责检查依赖、源码和验证证据，不把功能测试结果视为漏洞扫描结果。

2026-09-16 发布时，Jackson 2.18.9 查询没有匹配公告。OSV 在 2026-09 下旬和 10 月更新公告后，本次重新解析的 47 个 Maven 编译、运行与测试坐标中，Jackson core/databind 2.18.9 命中 7 项。审查人员没有复用旧报告的“未命中”结论。

## 审查人员实施的修复

| 组件 | 原版本 | 当前版本 | 审查人员验证结果 |
|---|---|---|---|
| 应用 Jackson BOM | 2.18.9 | 2.18.11 | Maven 同时解析 core、databind、annotations 为 2.18.11；47 个完整坐标 OSV 复查无匹配。 |
| 应用 OTel API/context/common | 1.64.0 | 1.65.0 | 应用 API 与 ADOT 2.31.0 内含 SDK 1.65.0 对齐。 |
| 官方 ADOT Java | 2.30.0 | 2.31.0 | 审查人员核对 GitHub release digest 和本地 JAR SHA-256；旧包 Netty 公告不再命中，新包残留 4 项 Jackson HIGH 公告。 |
| Python 管理脚本依赖 | 7 个固定版本 | 保持版本 | pip-audit 对 requirements.txt 7 项固定依赖查询无匹配。 |

ADOT 2.31.0 SHA-256：`4e0f171c6d935f0b9ae5f66b28b0b2dae5e2ac1261fc3e3ffc396ef1e500f4fc`。审查人员保留 AWS 官方 JAR，没有替换其中的 shaded 类。应用 Jackson BOM 升级不会修复 ADOT 独立类加载空间里的 Jackson 2.22.2。

## 官方 ADOT 2.31.0 的残留公告

OSV 对官方 JAR 保留的 Maven 元数据查询命中以下 4 项；4 项均为 HIGH，没有 CRITICAL。公告给出的 Jackson 2.22 维护线修复版本为 2.22.3。截至本次检查，GitHub 最新正式 ADOT Java 发行版为 2.31.0。

| 公告 | 漏洞触发条件 | 审查人员查到的当前应用边界 |
|---|---|---|
| GHSA-7hhh-6rmp-j9qf | Jackson `JsonFactory.createParser(DataInput)` 在读取恶意错误 token 时无限累加错误字符串，造成内存耗尽。 | 审查人员没有在 ADOT 非 Jackson 类中发现 DataInput parser 调用。ADOT 已识别 parser 调用使用 String、File 或 InputStream。Java HTTP 入口使用已修复的应用 Jackson `readTree(byte[])`。 |
| GHSA-p6pp-m3f8-5c89 | Jackson `NumberInput.looksLikeValidNumber()` 对特制长数字字符串执行二次复杂度正则；Jackson 数值反序列化器可能间接调用它。 | 审查人员发现内部 Double/Float/Number/BigDecimal 反序列化器存在调用，因此没有把“无外部直接调用”解释为不可达。ADOT 当前可识别读取点为资源元数据、打包 SQL 关键字、X-Ray 采样响应、采样/动态配置及 Lambda 适配。HTTP 入口只接受文本 prompt，并不把 prompt 交给这些数值绑定 API。 |
| GHSA-cxp5-3px4-pw24 | 应用把攻击者 JSON 反序列化到 `@JsonIdentityInfo` 集合或 map；反序解析 object-ID 引用造成二次 CPU 工作。 | 审查人员没有在 ADOT 非 Jackson 类和当前应用源码中发现 JsonIdentityInfo 引用；HTTP 入口只解析 JsonNode，不绑定身份对象集合。 |
| GHSA-wv8q-qhhj-9h54 | 应用开启 `@JsonTypeInfo(use=Id.NAME, defaultImpl=...)` 等 fallback，并用长寿命 mapper 接收大量未知 type ID，造成无限缓存增长。 | 审查人员没有在 ADOT 非 Jackson 类中发现 JsonTypeInfo、defaultImpl、enableDefaultTyping/activateDefaultTyping/setDefaultTyping 引用；当前应用也没有启用此配置。 |

审查人员解析了 JAR 的 26,967 个 class/classdata 条目，并去重整理了 33 个 Jackson 外部调用者。审查人员另外用 `javap -c -p` 核对关键读取路径：`AwsSpanProcessingUtil.getDialectKeywords()` 读取 JAR 内 `configuration/sql_dialect_keywords.json`，先绑定 JsonNode，再读取 List；该方法不读取用户 prompt。`XraySamplerClient` 绑定服务采样响应，Application Signals 配置类绑定采样配置，DynamicInstrumentationClient 绑定动态配置；Lambda 序列化适配只适用于 Lambda 框架。

容器入口显式设置 `OTEL_TRACES_SAMPLER=always_on`、`OTEL_AWS_APPLICATION_SIGNALS_ENABLED=false`，当前服务采用 JDK HttpServer，而不是 Lambda。审查人员据此没有发现当前用户 HTTP 输入到上述残留漏洞的静态触发路径。运维人员若启用 X-Ray 动态采样、动态配置、Lambda 适配，或扩展 Jackson 类型绑定，应重新审查输入信任边界。

## 验证结果与限制

- 审查人员重新执行 Maven dependency:list 与 package：47 个坐标成功解析，当时已有 2 项 AgentContractTest 全部通过。新增 Runtime HTTP 测试由实现代理及 Supervisor 在最终验收记录中单独报告；本报告不预先把未运行测试写为通过。
- 审查人员执行既有 Python 脚本测试：首次缺少测试要求的 WORK_DIR 环境变量，22 项均在初始化阶段报错；补充工作区内 WORK_DIR 后，22 项全部通过。两个运行日志均保留。
- 审查人员执行 `bash -n scripts/run.sh`，语法检查通过。
- Gitleaks 8.30.1 对当时源码工作树扫描发现 0 项凭证泄露。后续新增或修改文件需要 Supervisor 在最终交付前再次扫描；该结果不是完整 Git 历史或云端凭证审查。
- pip-audit 2.10.1 使用 `--no-deps --disable-pip` 查询 requirements.txt 的全部 7 个固定版本；审查人员没有把审计工具自身环境视为应用依赖。
- 审查人员没有扫描完整操作系统、容器基础镜像、JDK、全部 Maven 构建插件依赖。ADOT 查询只覆盖保留 Maven 元数据的组件，不能涵盖全部 shaded 或丢失元数据的组件。
- 常量池和 javap 检查不是完整运行时调用图，不排除反射、运行时扩展、未来配置变化或未登记漏洞。本次审查没有进行拒绝服务攻击验证或渗透测试。

**生产建议：运维人员应跟踪 AWS ADOT 后续正式版本，待官方包包含已修复 Jackson 后重新核对摘要、查询全部依赖并验证遥测与 Evaluations；当前仍有 4 项残留 HIGH 公告，不能以“未发现当前触发路径”关闭依赖风险记录。**


## 封装重构后的补充检查

测试执行者为 AgentTelemetry 增加内存 SpanExporter 契约验证。Maven 新增两个 test-only 坐标：opentelemetry-sdk-trace 与 opentelemetry-sdk-common，均为 1.65.0；审查人员对这两个新坐标进行 OSV 增量查询，未匹配已知公告。完整 Maven 坐标数量因此由 47 增至 49，生产应用依赖保持相同，测试 SDK 不进入生产 JAR。

Amazon ECR 对首次 Runtime 镜像的基础扫描已完成，报告 2 项 MEDIUM 和 2 项 UNDEFINED 发现，未报告 HIGH/CRITICAL。MEDIUM 对应 FreeType CVE-2026-95512，UNDEFINED 对应 libpng USN-8899-1；源包与二进制包名称分别产生记录。Java 订单服务没有字体或 PNG 处理入口，运维人员仍需跟踪基础镜像修复。ECR 基础扫描不覆盖或抵消 ADOT JAR 的 4 项 HIGH 公告。

部署执行者将原始扫描响应、JAR 字节码分析、AWS 状态及遥测证据保存在本地交付目录，公开源码仅保留本报告中的匿名结论。测试执行者最终执行的全部测试数量、云端调用和评分结果，以 [Runtime 验证报告](e2e-test-report-runtime.md) 为准。
