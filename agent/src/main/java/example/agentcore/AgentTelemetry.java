package example.agentcore;

import io.opentelemetry.api.GlobalOpenTelemetry;
import io.opentelemetry.api.baggage.Baggage;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.SpanKind;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.api.trace.Tracer;
import io.opentelemetry.context.Scope;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.function.Supplier;
import software.amazon.awssdk.services.bedrockruntime.model.*;

/**
 * Java 应用的遥测适配器：本类使用标准 io.opentelemetry API，业务类无需直接操作 span 或 GenAI 字段。
 * 启动器挂载的外置 aws-opentelemetry-agent.jar 才是 ADOT Java agent；ADOT 提供共享 provider、
 * SDK、导出器和 SigV4 签名。应用不另建 SDK，也不自行发送遥测数据。
 * AgentCore Evaluations（Eval）随后按 scope、操作类型和内容字段读取导出的遥测，并由 evaluator 评判质量。
 */
public final class AgentTelemetry {
    // Eval 的通用 OpenTelemetry 适配按此前缀识别应用埋点；Java 应用使用固定 scope 标识本示例。
    static final String SCOPE_NAME = "opentelemetry.instrumentation.java_agentcore_demo";
    private final Tracer tracer;
    private final String sessionId;
    private final String modelId;
    private final String region;
    private final String agentName;
    // 此开关仅控制本类手工写入的内容字段；ADOT 自动埋点的内容采集由启动配置单独控制。
    private final boolean capture;

    /** Java 应用从 ADOT 注册的全局 provider 获取 tracer，确保手工 span 与自动埋点共享上下文。 */
    AgentTelemetry(String sessionId, String modelId, String region, String agentName, boolean capture) {
        this(sessionId, modelId, region, agentName, capture, GlobalOpenTelemetry.getTracer(SCOPE_NAME, "1.0.0"));
    }

    /** 本地契约测试注入独立 tracer；生产构造器始终复用 ADOT 的全局 provider。 */
    AgentTelemetry(String sessionId, String modelId, String region, String agentName, boolean capture, Tracer tracer) {
        this.sessionId = sessionId;
        this.modelId = modelId;
        this.region = region;
        this.agentName = agentName;
        this.capture = capture;
        this.tracer = tracer;
    }

    /** Java 编排层负责执行业务，并把回答与业务计数提交给当前 AgentRun。 */
    @FunctionalInterface
    interface AgentAction { void execute(AgentRun run); }

    /** Java 工具编排层接收已解码参数，返回成功结果或需要交给模型修正的可恢复错误。 */
    @FunctionalInterface
    interface ToolAction { ToolOutcome execute(Object arguments); }

    /** Java 工具编排层显式区分成功与可恢复失败；遥测适配器不会把可恢复失败抛给调用者。 */
    record ToolOutcome(Map<String, Object> output, RuntimeException error) {
        /** Java 工具编排层保留工具返回的原始字段。 */
        static ToolOutcome success(Map<String, Object> output) { return new ToolOutcome(output, null); }
        /** Java 工具编排层保留错误响应和异常，分别供 Bedrock 模型与 ADOT 遥测使用。 */
        static ToolOutcome failure(Map<String, Object> output, RuntimeException error) { return new ToolOutcome(output, error); }
    }

    /**
     * Java 遥测适配器管理根 span 与会话 baggage；Java 编排层负责凭证、Bedrock 客户端和必要工具校验。
     * 只有业务回调成功返回后，适配器才输出 agent_complete 并标记 OK。
     */
    Map<String, Object> agent(String task, AgentAction action) {
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
                AgentRun run = new AgentRun(root);
                action.execute(run);
                if (run.answer == null) throw new IllegalStateException("Agent callback did not provide an outcome");
                root.setStatus(StatusCode.OK);
                return run.result();
            } catch (RuntimeException e) { failed(root, e); throw e; }
            finally { root.end(); } // Java 应用结束 span；ADOT provider 负责批量导出及进程退出时的刷出。
        }
    }

    /** Java 遥测适配器保存本次结果统计；业务回调仍负责决定这些结果是否满足验收条件。 */
    final class AgentRun {
        private final Span root;
        private String answer;
        private int modelCalls;
        private List<String> successfulTools;

        /** 遥测适配器把统计对象绑定到本次根 span，禁止跨请求共享。 */
        private AgentRun(Span root) { this.root = root; }

        /** Java 编排层先记录业务结果，再校验必要工具；本方法不输出成功日志，也不设置 OK。 */
        void outcome(String answer, int modelCalls, List<String> successfulTools, boolean usedBoth) {
            this.answer = answer;
            this.modelCalls = modelCalls;
            this.successfulTools = List.copyOf(successfulTools);
            if (capture) root.setAttribute("gen_ai.task.output", answer);
            root.setAttribute("demo.model_calls", modelCalls);
            root.setAttribute("demo.successful_tool_calls", successfulTools.size());
            root.setAttribute("demo.required_tools_observed", usedBoth);
        }

        /** 遥测适配器在业务成功后保留原 CLI 日志与 HTTP Map 返回契约。 */
        private Map<String, Object> result() {
            var result = new java.util.LinkedHashMap<String, Object>();
            result.put("event", "agent_complete"); result.put("timestamp", Instant.now().toString());
            result.put("session_id", sessionId); result.put("trace_id", root.getSpanContext().getTraceId());
            result.put("root_span_id", root.getSpanContext().getSpanId()); result.put("model_id", modelId);
            result.put("model_calls", modelCalls); result.put("successful_tools", successfulTools);
            result.put("capture_content", capture);
            if (capture) result.put("answer", answer);
            System.out.println(Tools.json(result));
            // Java HTTP 服务必须向调用者返回回答，即使操作者关闭了遥测内容采集。
            result.put("answer", answer);
            return Map.copyOf(result);
        }
    }

    /**
     * Java 遥测适配器把实际 Converse 请求、响应和用量写入 chat span；业务回调负责执行真实 API 调用。
     * 适配器读取业务层构造的请求配置，避免模型参数与 span 中的值发生分歧。
     */
    ConverseResponse chat(ConverseRequest request, Supplier<ConverseResponse> action) {
        Span chat = span("chat", request.modelId(), SpanKind.CLIENT);
        try (Scope current = chat.makeCurrent()) {
            chat.setAttribute("gen_ai.request.model", request.modelId());
            chat.setAttribute("gen_ai.request.max_tokens", request.inferenceConfig().maxTokens().longValue());
            chat.setAttribute("gen_ai.request.temperature", request.inferenceConfig().temperature().doubleValue());
            chat.setAttribute("server.address", "bedrock-runtime." + region + ".amazonaws.com");
            chat.setAttribute("aws.region", region);
            if (capture) {
                // Eval 按标准字段读取推理消息及系统指令；Java 应用将标准结构序列化为 span 属性字符串。
                chat.setAttribute("gen_ai.input.messages", Tools.json(messages(request.messages())));
                chat.setAttribute("gen_ai.system_instructions", Tools.json(request.system().stream()
                    .map(instruction -> Map.of("type", "text", "content", instruction.text())).toList()));
                chat.setAttribute("gen_ai.tool.definitions", Tools.json(Tools.DEFINITIONS));
            }
            ConverseResponse response = action.get();
            chat.setAttribute("gen_ai.response.model", request.modelId()); // Converse 未返回解析后的模型版本，Java 应用沿用请求中的模型 ID。
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

    /** Java 遥测适配器记录工具调用并映射 Bedrock toolResult；业务回调负责本地工具执行和可恢复错误选择。 */
    ContentBlock tool(ToolUseBlock call, ToolAction action) {
        Span tool = span("execute_tool", call.name(), SpanKind.INTERNAL);
        try (Scope current = tool.makeCurrent()) {
            // Eval 从 name、call.arguments、call.result 读取工具信息；Java 应用保留 call.id 关联请求与结果。
            tool.setAttribute("gen_ai.tool.name", call.name());
            tool.setAttribute("gen_ai.tool.type", "function");
            tool.setAttribute("gen_ai.tool.call.id", call.toolUseId());
            Object args = Tools.value(call.input());
            if (capture) tool.setAttribute("gen_ai.tool.call.arguments", Tools.json(args));
            ToolOutcome outcome = action.execute(args);
            Map<String, Object> output = outcome.output();
            ToolResultStatus status;
            if (outcome.error() == null) {
                status = ToolResultStatus.SUCCESS;
                tool.setStatus(StatusCode.OK);
            } else {
                status = ToolResultStatus.ERROR;
                failed(tool, outcome.error());
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

}
