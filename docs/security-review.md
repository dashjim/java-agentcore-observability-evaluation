# 发布前安全检查

审查日期：2026-09-16。审查对象是本仓库准备上传的源码、说明、匿名样例及解析后的应用依赖；实验执行者将真实 AWS 状态、账户标识、原始遥测和构建缓存保留在仓库之外。

## 审查人员修复的依赖

| 依赖 | 原版本 | 发布版本 | 审查结果 |
|---|---|---|---|
| Jackson databind／core／annotations | 2.18.3 | 统一 BOM 2.18.9 | 原直接依赖查询出现 7 条已知安全公告；新版本查询未命中。 |
| OpenTelemetry API／context | 1.49.0 | 1.64.0 | 审查人员修复 W3C baggage 内存分配公告 GHSA-rcgg-9c38-7xpx，并与 ADOT 内含 SDK 对齐。 |
| urllib3 | 2.6.3 | 2.8.0 | pip-audit 原来返回 4 条匹配记录、对应 2 个唯一 PYSEC 公告；审查人员升级后复查全部 Python 运行依赖。 |

## 审查方法与边界

审查人员对 Maven 完整解析的 47 个编译／运行／测试依赖坐标调用 OSV 查询。修复后的坐标未命中已知漏洞。审查人员另用 pip-audit 审查 requirements.txt 中固定的 7 个 Python 运行依赖，修复后的全部固定版本未命中已知漏洞；审计工具环境的 pip 也已升级到 26.2.1。

审查人员使用 Gitleaks 8.30.1 检查源文件与最终 Git 提交，并人工检查提交清单中的账号、主机、绝对本机路径、凭证、原始会话和 state。Gitleaks 二进制下载后与 GitHub release asset 的 SHA-256 核对。启动脚本对 ADOT 2.30.0 和 Maven 3.9.11 下载分别校验固定 SHA-256 与 SHA-512。

应用使用 AWS 默认凭证链；仓库没有静态 Access Key。配置脚本将 Eval 服务角色限制在指定账号、Region、来源日志、结果日志及 judge 模型；目录查询 `DescribeLogGroups` 的目录 ARN 与日志内容读取权限分开。Java 运行身份仍由管理员配置，setup 不扩大现有 instance role 的权限。

配置脚本在创建前检查重名和远端归属标签，并原子保存操作状态。应用不会自动回滚删除；受保留标记保护的 state 在 cleanup 初始化 AWS 客户端之前被拒绝。14 项本地安全测试覆盖保留、防覆盖、失败恢复、归属验证、权限边界、资源级日志策略 API 参数和错误日志处理。测试替身不访问 AWS。

本次检查没有覆盖全部 Maven 构建插件依赖、操作系统、JDK 或 ADOT shaded JAR 内全部第三方组件，也不是渗透测试。未命中已知公告只表示检查时数据库没有匹配。管理员应在后续发布时重新审查依赖与实际 IAM 配置。

## 数据与资源保留

Java 应用记录的示例只包含本地虚构订单。原始 telemetry 仍可能包含账号、主机、进程参数、模型输入输出；实验执行者不会把这些原始文件上传 GitHub。匿名样例的替换字段由 examples/evidence/README.md 说明。

本次 AWS 来源日志和结果日志均为 Never expire，Online 配置保持启用。实验执行者保留成功和失败事件、自定义 evaluator、角色与策略，直到用户明确要求删除。Git 忽略规则只控制提交范围，不删除本地或云端证据。


## Dashboard 扩展检查

审查人员检查新增 Dashboard 脚本的账号核对、同名资源保护和历史窗口参数。脚本只读取已有配置并创建 CloudWatch Dashboard，不修改 IAM、AgentCore 配置或日志，不开启公开共享。8 项 Dashboard 测试通过，原有 14 项脚本测试仍通过；源码、JSON 示例和 Git 提交再次执行秘密检查。

本次扩展没有新增依赖。开发者继续使用此前审查过的 47 个 Maven 坐标与 7 个固定 Python 运行依赖。实验执行者只把匿名指标图片和占位符 Dashboard 定义加入 Git；实际资源 JSON、账号信息与 API 回执保留在仓库外。
