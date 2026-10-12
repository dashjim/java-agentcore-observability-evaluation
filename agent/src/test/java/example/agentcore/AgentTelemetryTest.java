package example.agentcore;

import static org.junit.jupiter.api.Assertions.*;

import io.opentelemetry.api.OpenTelemetry;
import io.opentelemetry.api.baggage.Baggage;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.SpanKind;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.sdk.common.CompletableResultCode;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.opentelemetry.sdk.trace.export.SpanExporter;
import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collection;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.atomic.AtomicBoolean;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import software.amazon.awssdk.awscore.AwsResponseMetadata;
import software.amazon.awssdk.services.bedrockruntime.model.*;

/**
 * 测试执行者用 test-only OTel SDK 和内存 exporter 验证应用 span 契约，不创建 AWS 客户端。
 * 测试数据只模拟 Bedrock 消息；生产 JAR 不包含本测试 SDK，ADOT 云端导出另由真实 Runtime 验证。
 */
class AgentTelemetryTest {
    private static final String SESSION = "java-telemetry-contract-session-001";
    private static final String MODEL = "amazon.nova-lite-v1:0";
    private static final String TASK = "private-task 查询订单 ORD-1001";
    private static final String ANSWER = "private-answer ORD-1001 SHIPPED 73.5 USD";
    private final List<SpanData> exported = new ArrayList<>();
    private SdkTracerProvider provider;

    /** 测试执行者为每个测试创建独立 SDK，避免改变生产代码依赖的 GlobalOpenTelemetry。 */
    @BeforeEach
    void startProvider() {
        provider = SdkTracerProvider.builder().addSpanProcessor(SimpleSpanProcessor.create(new SpanExporter() {
            /** 测试 exporter 保存 SDK 完成后的不可变 SpanData，供断言检查实际字段与生命周期。 */
            @Override public CompletableResultCode export(Collection<SpanData> spans) {
                exported.addAll(spans);
                return CompletableResultCode.ofSuccess();
            }
            /** 内存 exporter 没有异步队列，因此刷新立即完成。 */
            @Override public CompletableResultCode flush() { return CompletableResultCode.ofSuccess(); }
            /** 测试执行者关闭 SDK 时无需处理网络资源。 */
            @Override public CompletableResultCode shutdown() { return CompletableResultCode.ofSuccess(); }
        })).build();
    }

    /** 测试执行者关闭独立 provider，禁止测试资源或当前 scope 泄漏到后续测试。 */
    @AfterEach
    void closeProvider() { provider.close(); }

    /** 测试执行者只注入 tracer；内容策略与生产构造器具有相同含义。 */
    private AgentTelemetry telemetry(boolean capture) {
        return new AgentTelemetry(SESSION, MODEL, "us-east-1", "contract-agent", capture,
            provider.get(AgentTelemetry.SCOPE_NAME, "1.0.0"));
    }

    /** 测试执行者按真实业务格式构造请求，但不执行网络调用。 */
    private ConverseRequest request(List<Message> history) {
        return ConverseRequest.builder().modelId(MODEL).messages(history)
            .system(SystemContentBlock.fromText(MinimalAgent.SYSTEM_PROMPT)).toolConfig(Tools.configuration())
            .inferenceConfig(InferenceConfiguration.builder().maxTokens(700).temperature(0.0f).build()).build();
    }

    /** 测试执行者用 SDK 对象模拟模型响应，保留请求 ID、停止原因及 token 用量。 */
    private ConverseResponse response(ContentBlock content, StopReason reason, String requestId) {
        var builder = ConverseResponse.builder().output(ConverseOutput.fromMessage(Message.builder()
            .role(ConversationRole.ASSISTANT).content(content).build())).stopReason(reason)
            .usage(TokenUsage.builder().inputTokens(21).outputTokens(9).totalTokens(30).build());
        // SDK 的元数据基类提供受保护构造器；测试用匿名子类承载固定请求 ID，不创建客户端。
        builder.responseMetadata(new AwsResponseMetadata(Map.of("AWS_REQUEST_ID", requestId)) {});
        return builder.build();
    }

    /** 测试执行者模拟三轮模型响应并执行两个真实本地工具，产生与订单流程相同的六个业务 span。 */
    private Map<String, Object> orderScenario(AgentTelemetry telemetry) {
        return telemetry.agent(TASK, run -> {
            assertEquals(SESSION, Baggage.current().getEntryValue("session.id"));
            List<Message> history = new ArrayList<>();
            history.add(Message.builder().role(ConversationRole.USER).content(ContentBlock.fromText(TASK)).build());
            var lookup = ToolUseBlock.builder().name("order_lookup").toolUseId("lookup-1")
                .input(Tools.document(Map.of("order_id", "ORD-1001"))).build();
            var calculator = ToolUseBlock.builder().name("calculator").toolUseId("calculator-2")
                .input(Tools.document(Map.of("operation", "multiply", "a", 24.5, "b", 3))).build();
            List<String> tools = new ArrayList<>();
            for (var call : List.of(lookup, calculator)) {
                var answer = telemetry.chat(request(history), () -> response(ContentBlock.fromToolUse(call), StopReason.TOOL_USE, call.toolUseId()));
                history.add(answer.output().message());
                var toolResult = telemetry.tool(call, args -> {
                    var output = Tools.execute(call.name(), Tools.JSON.valueToTree(args));
                    tools.add(call.name());
                    return AgentTelemetry.ToolOutcome.success(output);
                });
                history.add(Message.builder().role(ConversationRole.USER).content(toolResult).build());
            }
            telemetry.chat(request(history), () -> response(ContentBlock.fromText(ANSWER), StopReason.END_TURN, "final-3"));
            run.outcome(ANSWER, 3, tools, true);
        });
    }

    /** 测试执行者按业务 operation 读取 exporter 的真实 SpanData。 */
    private List<SpanData> spans(String operation) {
        return exported.stream().filter(span -> operation.equals(text(span, "gen_ai.operation.name"))).toList();
    }

    /** 测试执行者统一读取字符串属性，避免把不同类型属性误当文本。 */
    private static String text(SpanData span, String key) { return span.getAttributes().get(AttributeKey.stringKey(key)); }

    /** 测试执行者核对六个 span 的父子关系、完整业务字段、工具映射和响应标识。 */
    @Test
    void sixSpansPreserveEvaluationContractAndContext() throws Exception {
        var result = orderScenario(telemetry(true));
        assertEquals(6, exported.size());
        assertEquals(3, spans("chat").size());
        assertEquals(2, spans("execute_tool").size());
        var root = spans("invoke_agent").getFirst();
        assertFalse(root.getParentSpanContext().isValid());
        assertEquals(result.get("trace_id"), root.getTraceId());
        assertEquals(result.get("root_span_id"), root.getSpanId());
        assertEquals(SESSION, result.get("session_id"));
        assertEquals(ANSWER, result.get("answer"));
        assertEquals(List.of("order_lookup", "calculator"), result.get("successful_tools"));
        assertEquals(TASK, text(root, "gen_ai.task.input"));
        assertEquals(ANSWER, text(root, "gen_ai.task.output"));
        assertEquals(3L, root.getAttributes().get(AttributeKey.longKey("demo.model_calls")));
        assertEquals(2L, root.getAttributes().get(AttributeKey.longKey("demo.successful_tool_calls")));
        assertEquals(true, root.getAttributes().get(AttributeKey.booleanKey("demo.required_tools_observed")));
        assertEquals(Tools.JSON.valueToTree(Tools.DEFINITIONS), Tools.JSON.readTree(text(root, "gen_ai.tool.definitions")));
        for (var span : exported) {
            assertTrue(span.hasEnded());
            assertEquals(StatusCode.OK, span.getStatus().getStatusCode());
            assertEquals(root.getTraceId(), span.getTraceId());
            assertEquals(SESSION, text(span, "session.id"));
            assertEquals(SESSION, text(span, "gen_ai.conversation.id"));
            assertEquals("contract-agent", text(span, "gen_ai.agent.name"));
            assertEquals("aws.bedrock", text(span, "gen_ai.provider.name"));
            assertEquals("aws.bedrock", text(span, "gen_ai.system"));
            assertEquals(AgentTelemetry.SCOPE_NAME, span.getInstrumentationScopeInfo().getName());
            if (span != root) assertEquals(root.getSpanId(), span.getParentSpanId());
        }
        for (var chat : spans("chat")) {
            assertEquals(SpanKind.CLIENT, chat.getKind());
            assertEquals(MODEL, text(chat, "gen_ai.request.model"));
            assertEquals(MODEL, text(chat, "gen_ai.response.model"));
            assertEquals(700L, chat.getAttributes().get(AttributeKey.longKey("gen_ai.request.max_tokens")));
            assertEquals(0.0, chat.getAttributes().get(AttributeKey.doubleKey("gen_ai.request.temperature")));
            assertEquals(21L, chat.getAttributes().get(AttributeKey.longKey("gen_ai.usage.input_tokens")));
            assertEquals(9L, chat.getAttributes().get(AttributeKey.longKey("gen_ai.usage.output_tokens")));
            assertEquals("bedrock-runtime.us-east-1.amazonaws.com", text(chat, "server.address"));
            assertEquals("us-east-1", text(chat, "aws.region"));
            assertNotNull(text(chat, "aws.request_id"));
            assertEquals(MinimalAgent.SYSTEM_PROMPT, Tools.JSON.readTree(text(chat, "gen_ai.system_instructions")).at("/0/content").asText());
        }
        var finalChat = spans("chat").getLast();
        assertEquals(List.of("end_turn"), finalChat.getAttributes().get(AttributeKey.stringArrayKey("gen_ai.response.finish_reasons")));
        assertEquals(ANSWER, Tools.JSON.readTree(text(finalChat, "gen_ai.output.messages")).at("/0/parts/0/content").asText());
        var input = Tools.JSON.readTree(text(finalChat, "gen_ai.input.messages"));
        assertEquals("tool", input.at("/2/role").asText());
        assertEquals("lookup-1", input.at("/2/parts/0/id").asText());
        var calculator = spans("execute_tool").getLast();
        assertEquals(SpanKind.INTERNAL, calculator.getKind());
        assertEquals("calculator", text(calculator, "gen_ai.tool.name"));
        assertEquals("function", text(calculator, "gen_ai.tool.type"));
        assertEquals("calculator-2", text(calculator, "gen_ai.tool.call.id"));
        assertEquals(24.5, Tools.JSON.readTree(text(calculator, "gen_ai.tool.call.arguments")).path("a").asDouble());
        assertEquals(73.5, Tools.JSON.readTree(text(calculator, "gen_ai.tool.call.result")).path("result").asDouble());
        assertNull(Baggage.current().getEntryValue("session.id"));
        assertFalse(Span.current().getSpanContext().isValid());
    }

    /** 测试执行者关闭内容采集后，响应仍含回答，但所有 span 的业务内容字段都必须缺席。 */
    @Test
    void captureDisabledOmitsContentWithoutChangingReturnContract() {
        var result = orderScenario(telemetry(false));
        assertEquals(ANSWER, result.get("answer"));
        assertEquals(false, result.get("capture_content"));
        Set<String> content = Set.of("gen_ai.task.input", "gen_ai.task.output", "gen_ai.tool.definitions",
            "gen_ai.input.messages", "gen_ai.output.messages", "gen_ai.system_instructions",
            "gen_ai.tool.call.arguments", "gen_ai.tool.call.result");
        assertEquals(6, exported.size());
        for (var span : exported) {
            span.getAttributes().forEach((key, value) -> assertFalse(content.contains(key.getKey()), key.getKey()));
            assertEquals(SESSION, text(span, "session.id"));
        }
    }

    /** 测试执行者让业务校验在 outcome 后失败，确保适配器保留计数、标记失败且不提前输出成功事件。 */
    @Test
    void outcomeDoesNotAnnounceSuccessBeforeBusinessValidation() {
        var output = new ByteArrayOutputStream();
        PrintStream original = System.out;
        try (var captureOutput = new PrintStream(output, true, StandardCharsets.UTF_8)) {
            System.setOut(captureOutput);
            assertThrows(IllegalStateException.class, () -> telemetry(true).agent(TASK, run -> {
                run.outcome("incomplete-answer", 1, List.of("order_lookup"), false);
                throw new IllegalStateException("required-tool-missing");
            }));
        } finally {
            System.setOut(original);
        }
        assertFalse(output.toString(StandardCharsets.UTF_8).contains("agent_complete"));
        var root = exported.getFirst();
        assertEquals(StatusCode.ERROR, root.getStatus().getStatusCode());
        assertEquals(false, root.getAttributes().get(AttributeKey.booleanKey("demo.required_tools_observed")));
        assertEquals(1, root.getEvents().size());
        assertEquals("exception", root.getEvents().getFirst().getName());
        assertTrue(root.hasEnded());
    }

    /** 测试执行者注入模型调用异常，验证 chat/root 都结束且异常内容关闭时不进入 span。 */
    @Test
    void modelFailureMarksBothSpansAndRestoresContextWithoutCapturingDetails() {
        var telemetry = telemetry(false);
        var failure = new IllegalStateException("private-exception-detail");
        var actual = assertThrows(IllegalStateException.class, () -> telemetry.agent(TASK, run ->
            telemetry.chat(request(List.of(Message.builder().role(ConversationRole.USER)
                .content(ContentBlock.fromText(TASK)).build())), () -> { throw failure; })));
        assertSame(failure, actual);
        assertEquals(2, exported.size());
        for (var span : exported) {
            assertTrue(span.hasEnded());
            assertEquals(StatusCode.ERROR, span.getStatus().getStatusCode());
            assertEquals("IllegalStateException", span.getStatus().getDescription());
            assertEquals(IllegalStateException.class.getName(), text(span, "error.type"));
            assertTrue(span.getEvents().isEmpty());
        }
        assertNull(Baggage.current().getEntryValue("session.id"));
        assertFalse(Span.current().getSpanContext().isValid());
    }

    /** 测试执行者验证可恢复工具错误仍返回给模型，工具 span ERROR 不强制把整个 Agent 标记失败。 */
    @Test
    void recoverableToolFailureReturnsErrorResultAndKeepsParentUsable() {
        var telemetry = telemetry(true);
        var call = ToolUseBlock.builder().name("calculator").toolUseId("bad-call")
            .input(Tools.document(Map.of("operation", "divide", "a", 1, "b", 0))).build();
        var result = telemetry.agent(TASK, run -> {
            var response = telemetry.tool(call, args -> AgentTelemetry.ToolOutcome.failure(
                Map.of("error", "division-by-zero"), new ArithmeticException("division-by-zero")));
            assertEquals(ToolResultStatus.ERROR, response.toolResult().status());
            assertEquals("bad-call", response.toolResult().toolUseId());
            run.outcome("model-may-recover", 1, List.of(), false);
        });
        assertEquals("model-may-recover", result.get("answer"));
        assertEquals(StatusCode.ERROR, spans("execute_tool").getFirst().getStatus().getStatusCode());
        assertEquals(1, spans("execute_tool").getFirst().getEvents().size());
        assertEquals(StatusCode.OK, spans("invoke_agent").getFirst().getStatus().getStatusCode());
    }

    /** 测试执行者确认缺少记录 provider 时业务回调根本不执行，避免真实模型调用没有可评估 trace。 */
    @Test
    void missingProviderRejectsBeforeBusinessExecution() {
        var called = new AtomicBoolean();
        var noProvider = new AgentTelemetry(SESSION, MODEL, "us-east-1", "contract-agent", true,
            OpenTelemetry.noop().getTracer("no-provider"));
        assertThrows(IllegalStateException.class, () -> noProvider.agent(TASK, run -> called.set(true)));
        assertFalse(called.get());
        assertTrue(exported.isEmpty());
        assertNull(Baggage.current().getEntryValue("session.id"));
    }
}
