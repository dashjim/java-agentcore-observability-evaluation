package example.agentcore;

import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import software.amazon.awssdk.auth.credentials.DefaultCredentialsProvider;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.bedrockruntime.BedrockRuntimeClient;
import software.amazon.awssdk.services.bedrockruntime.model.*;

/**
 * 最小订单 Agent：Java 应用编排对话、执行本地工具并校验流程；真实 Bedrock 模型选择工具及参数、生成最终回答。
 *
 * <p>Java 业务代码通过 AgentTelemetry 的 agent/chat/tool 三个入口记录遥测。
 * AgentTelemetry 使用标准 OpenTelemetry API；外置 ADOT Java agent 负责共享 provider 和导出。
 */
public final class MinimalAgent {
    static final String DEFAULT_TASK = "查询订单 ORD-1001，用 calculator 计算单价乘数量，回答订单状态与商品总价（USD）。";
    // Java 应用向模型声明工具使用要求；Bedrock 模型仍负责每一轮决策，Java 不预先伪造工具调用。
    static final String SYSTEM_PROMPT = "You are a concise order assistant. The application provides two real local tools. "
        + "Always call order_lookup before answering an order question. Always call calculator for arithmetic, using "
        + "the values returned by order_lookup. Never invent tool results. Answer in Chinese, cite the order ID, "
        + "status and total with currency. Explain that this is demo fixture data. Do not include tax or shipping.";
    // Java 应用从环境变量读取运行参数；构造器把会话标识交给遥测适配器关联根、模型和工具 span。
    private final String modelId = env("BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0");
    private final String region = env("AWS_REGION", env("AWS_DEFAULT_REGION", "us-east-1"));
    private final String agentName = env("OTEL_SERVICE_NAME", "java-agentcore-demo");
    // Java 应用仅持有业务化遥测入口；AgentTelemetry 读取调用方传入的内容采集策略。
    private final AgentTelemetry telemetry;
    private final int maxCalls = Integer.parseInt(env("MAX_MODEL_CALLS", "5"));
    // Java 应用记录已成功执行的工具和模型请求次数，用于流程验收；这些计数不代表 Eval 评分。
    private final List<String> successfulTools = new ArrayList<>();
    private int modelCalls;
    private boolean started;

    /** Java CLI 从环境变量选择会话；Runtime HTTP 入口使用显式会话构造器。 */
    public MinimalAgent() {
        this(env("SESSION_ID", "java-eval-" + UUID.randomUUID()));
    }

    /** Java HTTP 服务为每次请求创建独立 Agent，将 Runtime 转发的会话标识传给所有业务 span。 */
    public MinimalAgent(String sessionId) {
        if (sessionId == null || sessionId.isBlank()) throw new IllegalArgumentException("Session ID is required");
        this.telemetry = new AgentTelemetry(sessionId, modelId, region, agentName,
            Boolean.parseBoolean(env("CAPTURE_CONTENT", "true")));
    }

    /**
     * Java 入口输出工具 schema，或启动一次真实 Bedrock 对话；失败时输出结构化错误并以非零状态退出。
     *
     * @param args 单独传入 {@code --schema} 时仅输出本地定义，不创建 Agent 或 AWS 客户端；其他参数组成任务文本
     */
    public static void main(String[] args) {
        if (args.length == 1 && "--schema".equals(args[0])) {
            System.out.println(Tools.json(Tools.DEFINITIONS));
            return;
        }
        try { new MinimalAgent().run(args.length == 0 ? DEFAULT_TASK : String.join(" ", args)); }
        catch (Exception e) {
            System.err.println(Tools.json(Map.of("event", "agent_failed", "error_type", e.getClass().getName(),
                "message", String.valueOf(e.getMessage()))));
            System.exit(1); // JVM 退出时由 ADOT 的 shutdown hook 刷出共享 provider 中待导出的遥测。
        }
    }

    /**
     * Java 应用管理凭证及 Bedrock 客户端的生命周期，并检查模型是否使用了两个必要工具。
     * AgentTelemetry 负责根 span、会话上下文及结构化日志，不参与业务正确性判断。
     *
     * @param task Java 应用交给 Bedrock 模型处理的用户任务
     * @return Java 应用的结构化执行结果；回答始终返回给调用者，内容采集开关仅控制日志和 span
     * @throws IllegalStateException ADOT 未记录 span，或模型结束时没有成功使用全部必要工具
     */
    public Map<String, Object> run(String task) {
        // Java 应用禁止重复使用同一业务实例；RuntimeServer 每次请求重新创建实例，隔离计数与工具状态。
        if (started) throw new IllegalStateException("Agent instances support a single invocation");
        started = true;
        if (maxCalls < 1 || maxCalls > 10) throw new IllegalArgumentException("MAX_MODEL_CALLS must be 1..10");
        return telemetry.agent(task, run -> {
            // 默认凭证链为 AWS SDK 提供凭证，SDK 为真实 Converse 请求签名；Java 应用管理超时与资源关闭。
            try (var credentials = DefaultCredentialsProvider.builder().build();
                 var client = BedrockRuntimeClient.builder().region(Region.of(region)).credentialsProvider(credentials)
                     .overrideConfiguration(c -> c.apiCallTimeout(Duration.ofSeconds(90))
                         .apiCallAttemptTimeout(Duration.ofSeconds(60))).build()) {
                String answer = converseLoop(client, task);
                // Java 仅校验两类工具都成功执行过；调用顺序、参数来源和回答质量仍需 Eval 或人工评判。
                boolean usedBoth = successfulTools.containsAll(List.of("order_lookup", "calculator"));
                run.outcome(answer, modelCalls, successfulTools, usedBoth);
                if (!usedBoth) throw new IllegalStateException("Model completed without both successful required tools");
            }
        });
    }

    /**
     * Java 应用维护 Bedrock 消息历史和调用上限，按模型的停止原因继续执行工具或返回模型回答。
     *
     * @param client Java 应用创建的真实 Bedrock Runtime 客户端
     * @param task 初始用户任务
     * @return Bedrock 模型在 END_TURN 中给出的非空文本
     * @throws IllegalStateException 模型响应不符合协议，或 Java 应用已耗尽调用预算
     */
    private String converseLoop(BedrockRuntimeClient client, String task) {
        List<Message> history = new ArrayList<>();
        history.add(Message.builder().role(ConversationRole.USER).content(ContentBlock.fromText(task)).build());
        for (int i = 0; i < maxCalls; i++) {
            ConverseResponse response = inference(client, history);
            Message assistant = response.output().message();
            history.add(assistant);
            if (response.stopReason() == StopReason.TOOL_USE) {
                // Bedrock 模型给出工具名、参数及调用 ID；Java 应用依次执行模型本轮请求的工具。
                List<ContentBlock> results = new ArrayList<>();
                for (ContentBlock block : assistant.content()) {
                    if (block.toolUse() != null) results.add(execute(block.toolUse()));
                }
                if (results.isEmpty()) throw new IllegalStateException("Bedrock reported tool_use without tool calls");
                // Bedrock Converse 协议要求 Java 应用以 USER 角色提交 toolResult，模型在下一轮读取真实结果。
                history.add(Message.builder().role(ConversationRole.USER).content(results).build());
            } else if (response.stopReason() == StopReason.END_TURN) {
                String answer = assistant.content().stream().filter(b -> b.text() != null).map(ContentBlock::text)
                    .collect(java.util.stream.Collectors.joining("\n"));
                if (answer.isBlank()) throw new IllegalStateException("Bedrock returned an empty final answer");
                return answer;
            } else throw new IllegalStateException("Bedrock did not complete: " + response.stopReasonAsString());
        }
        throw new IllegalStateException("Model call limit reached: " + maxCalls);
    }

    /**
     * Java 应用调用一次真实 Converse API；AgentTelemetry 将请求、响应和用量交给 ADOT 导出，供 Eval 读取。
     *
     * @param client 执行网络请求的 Bedrock Runtime 客户端
     * @param history Java 应用累积的用户消息、模型消息和工具结果
     * @return Bedrock 模型响应，包含工具调用或最终回答及停止原因
     */
    private ConverseResponse inference(BedrockRuntimeClient client, List<Message> history) {
        // Java 应用提供工具 schema 和历史；Bedrock 模型决定是否调用工具以及最终回答的内容。
        var request = ConverseRequest.builder().modelId(modelId)
            .system(SystemContentBlock.fromText(SYSTEM_PROMPT)).messages(history).toolConfig(Tools.configuration())
            .inferenceConfig(InferenceConfiguration.builder().maxTokens(700).temperature(0.0f).build()).build();
        return telemetry.chat(request, () -> {
            modelCalls++;
            return client.converse(request);
        });
    }

    /**
     * Java 应用验证并执行模型请求的本地工具，将成功或可恢复错误作为 toolResult 交还 Bedrock 模型。
     *
     * @param call Bedrock 模型生成的工具调用，含名称、参数和 toolUseId
     * @return Java 应用生成的工具结果内容块，保留模型给出的调用 ID
     */
    private ContentBlock execute(ToolUseBlock call) {
        return telemetry.tool(call, args -> {
            try {
                var output = Tools.execute(call.name(), Tools.JSON.valueToTree(args));
                successfulTools.add(call.name());
                return AgentTelemetry.ToolOutcome.success(output);
            } catch (IllegalArgumentException | ArithmeticException e) {
                // Java 应用把参数/算术错误反馈给模型，让模型在剩余调用预算内决定是否修正并重试。
                return AgentTelemetry.ToolOutcome.failure(Map.of("error", String.valueOf(e.getMessage())), e);
            }
        });
    }

    /**
     * Java 应用读取环境变量；未设置或仅含空白时采用默认值。
     *
     * @param name 环境变量名
     * @param fallback 缺少有效配置时的默认值
     * @return 原始非空配置或默认值
     */
    private static String env(String name, String fallback) {
        String value = System.getenv(name);
        return value == null || value.isBlank() ? fallback : value;
    }
}
