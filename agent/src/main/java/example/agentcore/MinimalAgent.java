package example.agentcore;

import io.opentelemetry.api.GlobalOpenTelemetry;
import io.opentelemetry.api.baggage.Baggage;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.SpanKind;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.api.trace.Tracer;
import io.opentelemetry.context.Scope;
import java.time.Duration;
import java.time.Instant;
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
 * <p>Java 应用通过 OpenTelemetry API 写入 span；启动时挂载的 ADOT Java agent 提供共享
 * provider、SDK、导出器和 SigV4 签名。应用不另建 SDK，也不自行发送遥测数据。
 * AgentCore Evaluations（Eval）随后按 scope、操作类型和内容字段读取导出的遥测，并由 evaluator 评判质量。
 */
public final class MinimalAgent {
    // Eval 的通用 OpenTelemetry 适配按此前缀识别应用埋点；Java 应用使用固定 scope 标识本示例。
    static final String SCOPE_NAME = "opentelemetry.instrumentation.java_agentcore_demo";
    static final String DEFAULT_TASK = "查询订单 ORD-1001，用 calculator 计算单价乘数量，回答订单状态与商品总价（USD）。";
    // Java 应用向模型声明工具使用要求；Bedrock 模型仍负责每一轮决策，Java 不预先伪造工具调用。
    static final String SYSTEM_PROMPT = "You are a concise order assistant. The application provides two real local tools. "
        + "Always call order_lookup before answering an order question. Always call calculator for arithmetic, using "
        + "the values returned by order_lookup. Never invent tool results. Answer in Chinese, cite the order ID, "
        + "status and total with currency. Explain that this is demo fixture data. Do not include tax or shipping.";
    // Java 应用从 ADOT 注册的全局 provider 获取 tracer，确保手工 span 与自动埋点共享上下文。
    private final Tracer tracer = GlobalOpenTelemetry.getTracer(SCOPE_NAME, "1.0.0");
    // Java 应用从环境变量读取运行参数；sessionId 关联同一次会话的根 span、模型 span 和工具 span。
    private final String sessionId = env("SESSION_ID", "java-eval-" + UUID.randomUUID());
    private final String modelId = env("BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0");
    private final String region = env("AWS_REGION", env("AWS_DEFAULT_REGION", "us-east-1"));
    private final String agentName = env("OTEL_SERVICE_NAME", "java-agentcore-demo");
    // 此开关仅控制本类手工写入的内容字段；ADOT 自动埋点的内容采集由启动配置单独控制。
    private final boolean capture = Boolean.parseBoolean(env("CAPTURE_CONTENT", "true"));
    private final int maxCalls = Integer.parseInt(env("MAX_MODEL_CALLS", "5"));
    // Java 应用记录已成功执行的工具和模型请求次数，用于流程验收；这些计数不代表 Eval 评分。
    private final List<String> successfulTools = new ArrayList<>();
    private int modelCalls;

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
     * Java 应用创建带会话信息的 span，ADOT provider 继承当前父上下文并负责后续导出。
     * Eval 按 {@code gen_ai.operation.name} 区分 invoke_agent、chat 与 execute_tool。
     *
     * @param operation Eval 识别的操作类型
     * @param suffix Java 应用附加到 span 名称的 Agent、模型或工具名称
     * @param kind OpenTelemetry 的 span 类型，与 Eval 的操作类型字段分开设置
     * @return 已开始的 span，调用方负责设置当前 scope 并结束 span
     */
    private Span span(String operation, String suffix, SpanKind kind) {
        return tracer.spanBuilder(operation + " " + suffix).setSpanKind(kind)
            .setAttribute("gen_ai.operation.name", operation)
            .setAttribute("session.id", sessionId).setAttribute("gen_ai.conversation.id", sessionId)
            .setAttribute("gen_ai.agent.name", agentName).setAttribute("gen_ai.provider.name", "aws.bedrock")
            .setAttribute("gen_ai.system", "aws.bedrock").startSpan();
    }

    /**
     * Java 应用管理根 span、凭证及 Bedrock 客户端的生命周期，并检查模型是否使用了两个必要工具。
     *
     * @param task Java 应用交给 Bedrock 模型处理的用户任务
     * @throws IllegalStateException ADOT 未记录 span，或模型结束时没有成功使用全部必要工具
     */
    private void run(String task) {
        if (maxCalls < 1 || maxCalls > 10) throw new IllegalArgumentException("MAX_MODEL_CALLS must be 1..10");
        // Java 应用在当前作用域放入会话 baggage；ADOT 自动埋点可沿同一上下文关联底层调用。
        try (Scope baggage = Baggage.current().toBuilder().put("session.id", sessionId).build().makeCurrent()) {
            Span root = span("invoke_agent", agentName, SpanKind.INTERNAL);
            try (Scope current = root.makeCurrent()) {
                // Java 应用在创建 AWS 客户端前拒绝无效/未采样的 provider，避免运行后缺少可评估的 trace。
                if (!root.isRecording() || !root.getSpanContext().isValid())
                    throw new IllegalStateException("No recording ADOT provider: launch with -javaagent and always_on sampling");
                if (capture) {
                    // Eval 从根 span 的 task.input/task.output 读取任务和回答；此处不包裹自定义消息信封。
                    root.setAttribute("gen_ai.task.input", task);
                    root.setAttribute("gen_ai.tool.definitions", Tools.json(Tools.DEFINITIONS));
                }
                System.out.println(Tools.json(Map.of("event", "agent_start", "timestamp", Instant.now().toString(),
                    "session_id", sessionId, "trace_id", root.getSpanContext().getTraceId(),
                    "root_span_id", root.getSpanContext().getSpanId(), "scope_name", SCOPE_NAME,
                    "model_id", modelId, "region", region, "capture_content", capture)));
                // 默认凭证链为 AWS SDK 提供凭证，SDK 为真实 Converse 请求签名；Java 应用管理超时与资源关闭。
                try (var credentials = DefaultCredentialsProvider.builder().build();
                     var client = BedrockRuntimeClient.builder().region(Region.of(region)).credentialsProvider(credentials)
                         .overrideConfiguration(c -> c.apiCallTimeout(Duration.ofSeconds(90))
                             .apiCallAttemptTimeout(Duration.ofSeconds(60))).build()) {
                    String answer = converseLoop(client, task);
                    if (capture) root.setAttribute("gen_ai.task.output", answer);
                    root.setAttribute("demo.model_calls", modelCalls);
                    root.setAttribute("demo.successful_tool_calls", successfulTools.size());
                    // Java 仅校验两类工具都成功执行过；调用顺序、参数来源和回答质量仍需 Eval 或人工评判。
                    boolean usedBoth = successfulTools.containsAll(List.of("order_lookup", "calculator"));
                    root.setAttribute("demo.required_tools_observed", usedBoth);
                    if (!usedBoth) throw new IllegalStateException("Model completed without both successful required tools");
                    root.setStatus(StatusCode.OK);
                    var result = new java.util.LinkedHashMap<String, Object>();
                    result.put("event", "agent_complete"); result.put("timestamp", Instant.now().toString());
                    result.put("session_id", sessionId); result.put("trace_id", root.getSpanContext().getTraceId());
                    result.put("root_span_id", root.getSpanContext().getSpanId()); result.put("model_id", modelId);
                    result.put("model_calls", modelCalls); result.put("successful_tools", successfulTools);
                    result.put("capture_content", capture);
                    if (capture) result.put("answer", answer);
                    System.out.println(Tools.json(result));
                }
            } catch (RuntimeException e) { failed(root, e); throw e; }
            finally { root.end(); } // Java 应用结束 span；ADOT provider 负责批量导出及进程退出时的刷出。
        }
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
     * Java 应用调用一次真实 Converse API，并将请求、响应和用量写入 chat span，供 ADOT 导出及 Eval 读取。
     *
     * @param client 执行网络请求的 Bedrock Runtime 客户端
     * @param history Java 应用累积的用户消息、模型消息和工具结果
     * @return Bedrock 模型响应，包含工具调用或最终回答及停止原因
     */
    private ConverseResponse inference(BedrockRuntimeClient client, List<Message> history) {
        Span chat = span("chat", modelId, SpanKind.CLIENT);
        try (Scope current = chat.makeCurrent()) {
            modelCalls++;
            chat.setAttribute("gen_ai.request.model", modelId);
            chat.setAttribute("gen_ai.request.max_tokens", 700L);
            chat.setAttribute("gen_ai.request.temperature", 0.0);
            chat.setAttribute("server.address", "bedrock-runtime." + region + ".amazonaws.com");
            chat.setAttribute("aws.region", region);
            if (capture) {
                // Eval 按标准字段读取推理消息及系统指令；Java 应用将标准结构序列化为 span 属性字符串。
                chat.setAttribute("gen_ai.input.messages", Tools.json(messages(history)));
                chat.setAttribute("gen_ai.system_instructions", Tools.json(List.of(Map.of("type", "text", "content", SYSTEM_PROMPT))));
                chat.setAttribute("gen_ai.tool.definitions", Tools.json(Tools.DEFINITIONS));
            }
            // Java 应用提供工具 schema 和历史；Bedrock 模型决定是否调用工具以及最终回答的内容。
            ConverseResponse response = client.converse(ConverseRequest.builder().modelId(modelId)
                .system(SystemContentBlock.fromText(SYSTEM_PROMPT)).messages(history).toolConfig(Tools.configuration())
                .inferenceConfig(InferenceConfiguration.builder().maxTokens(700).temperature(0.0f).build()).build());
            chat.setAttribute("gen_ai.response.model", modelId); // Converse 未返回解析后的模型版本，Java 应用沿用请求中的模型 ID。
            chat.setAttribute("aws.request_id", response.responseMetadata().requestId());
            chat.setAttribute(AttributeKey.stringArrayKey("gen_ai.response.finish_reasons"), List.of(response.stopReasonAsString()));
            if (response.usage() != null) {
                chat.setAttribute("gen_ai.usage.input_tokens", response.usage().inputTokens().longValue());
                chat.setAttribute("gen_ai.usage.output_tokens", response.usage().outputTokens().longValue());
            }
            if (capture) chat.setAttribute("gen_ai.output.messages", Tools.json(messages(List.of(response.output().message()))));
            chat.setStatus(StatusCode.OK);
            return response;
        } catch (RuntimeException e) { failed(chat, e); throw e; }
        finally { chat.end(); }
    }

    /**
     * Java 应用验证并执行模型请求的本地工具，将成功或可恢复错误作为 toolResult 交还 Bedrock 模型。
     *
     * @param call Bedrock 模型生成的工具调用，含名称、参数和 toolUseId
     * @return Java 应用生成的工具结果内容块，保留模型给出的调用 ID
     */
    private ContentBlock execute(ToolUseBlock call) {
        Span tool = span("execute_tool", call.name(), SpanKind.INTERNAL);
        try (Scope current = tool.makeCurrent()) {
            // Eval 从 name、call.arguments、call.result 读取工具信息；Java 应用保留 call.id 关联请求与结果。
            tool.setAttribute("gen_ai.tool.name", call.name());
            tool.setAttribute("gen_ai.tool.type", "function");
            tool.setAttribute("gen_ai.tool.call.id", call.toolUseId());
            Object args = Tools.value(call.input());
            if (capture) tool.setAttribute("gen_ai.tool.call.arguments", Tools.json(args));
            Map<String, Object> output;
            ToolResultStatus status;
            try {
                output = Tools.execute(call.name(), Tools.JSON.valueToTree(args));
                status = ToolResultStatus.SUCCESS;
                successfulTools.add(call.name());
                tool.setStatus(StatusCode.OK);
            } catch (IllegalArgumentException | ArithmeticException e) {
                // Java 应用把参数/算术错误反馈给模型，让模型在剩余调用预算内决定是否修正并重试。
                output = Map.of("error", String.valueOf(e.getMessage()));
                status = ToolResultStatus.ERROR;
                failed(tool, e);
            }
            if (capture) tool.setAttribute("gen_ai.tool.call.result", Tools.json(output));
            var event = new java.util.LinkedHashMap<String, Object>();
            event.put("event", "tool_executed"); event.put("session_id", sessionId);
            event.put("tool_name", call.name()); event.put("tool_call_id", call.toolUseId());
            event.put("status", status.toString());
            if (capture) { event.put("arguments", args); event.put("result", output); }
            System.out.println(Tools.json(event));
            return ContentBlock.fromToolResult(ToolResultBlock.builder().toolUseId(call.toolUseId()).status(status)
                .content(ToolResultContentBlock.fromJson(Tools.document(output))).build());
        } catch (RuntimeException e) { failed(tool, e); throw e; }
        finally { tool.end(); }
    }

    /**
     * Java 应用把 Bedrock 消息转换成供遥测使用的 GenAI role/parts 结构，不修改发送给模型的历史。
     * Bedrock 的 USER 消息承载工具结果，GenAI 遥测则以 tool 角色表示结果，便于下游关联工具调用。
     *
     * @param history Java 应用需要记录的 Bedrock 消息
     * @return 含文本、tool_call 或 tool_call_response 的标准消息列表，供 Eval 读取推理消息属性
     */
    static List<Map<String, Object>> messages(List<Message> history) {
        List<Map<String, Object>> output = new ArrayList<>();
        for (Message m : history) {
            List<Map<String, Object>> ordinary = new ArrayList<>();
            List<Map<String, Object>> results = new ArrayList<>();
            for (ContentBlock b : m.content()) {
                if (b.text() != null) ordinary.add(Map.of("type", "text", "content", b.text()));
                else if (b.toolUse() != null) {
                    var t = b.toolUse();
                    ordinary.add(Map.of("type", "tool_call", "id", t.toolUseId(), "name", t.name(), "arguments", Tools.value(t.input())));
                } else if (b.toolResult() != null) {
                    var t = b.toolResult();
                    List<Object> values = t.content().stream().map(c -> c.json() != null ? Tools.value(c.json()) : c.text()).toList();
                    results.add(Map.of("type", "tool_call_response", "id", t.toolUseId(),
                        "response", values.size() == 1 ? values.getFirst() : values));
                } else throw new IllegalArgumentException("This minimal agent only supports text and tool content blocks");
            }
            // 同一 Bedrock 消息可能混合文本和结果；Java 转换器按 GenAI 角色拆分，并保持工具调用 ID。
            if (!ordinary.isEmpty()) output.add(Map.of("role", m.roleAsString(), "parts", ordinary));
            if (!results.isEmpty()) output.add(Map.of("role", "tool", "parts", results));
        }
        return output;
    }

    /**
     * Java 应用标记失败 span；关闭内容采集时，本方法只写入错误类型，不记录异常详情或堆栈。
     *
     * @param span Java 应用需要标记的根、模型或工具 span
     * @param error 当前操作捕获的运行时异常
     */
    private void failed(Span span, RuntimeException error) {
        span.setStatus(StatusCode.ERROR, capture ? String.valueOf(error.getMessage()) : error.getClass().getSimpleName());
        span.setAttribute("error.type", error.getClass().getName());
        if (capture) span.recordException(error);
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
