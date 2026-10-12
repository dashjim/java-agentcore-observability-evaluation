package example.agentcore;

import com.fasterxml.jackson.core.JsonParser;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Semaphore;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.regex.Pattern;

/**
 * Java HTTP 入口实现 AgentCore Runtime 协议；Runtime 负责 IAM 验证，Java 应用负责参数校验与业务调用。
 * Java 服务按请求创建 MinimalAgent；ADOT 的进程级 provider 为全部请求提供共享遥测设施。
 */
public final class RuntimeServer implements AutoCloseable {
    static final String SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id";
    static final int MAX_BODY_BYTES = 65_536;
    static final int MAX_PROMPT_CHARS = 8_192;
    // Java 服务沿用 Runtime API 的会话范围，避免遥测会话与调用者传入的会话分离。
    private static final Pattern SESSION_PATTERN = Pattern.compile("[A-Za-z0-9][A-Za-z0-9_-]{32,255}");
    private static final ObjectMapper JSON = new ObjectMapper()
        .enable(JsonParser.Feature.STRICT_DUPLICATE_DETECTION)
        .enable(DeserializationFeature.FAIL_ON_TRAILING_TOKENS);
    private final HttpServer server;
    private final ExecutorService executor = Executors.newVirtualThreadPerTaskExecutor();
    private final Semaphore invocationSlots = new Semaphore(4);
    private final InvocationHandler handler;
    private final AtomicBoolean closed = new AtomicBoolean();

    /** 测试执行者可注入本地假业务函数；生产入口始终创建真实 MinimalAgent 并调用 Bedrock。 */
    @FunctionalInterface
    interface InvocationHandler {
        Map<String, Object> invoke(String sessionId, String prompt) throws Exception;
    }

    /** Java 服务允许测试选择随机本地端口；生产入口固定监听 0.0.0.0:8080。 */
    RuntimeServer(InetSocketAddress address, InvocationHandler handler) throws IOException {
        this.handler = handler;
        this.server = HttpServer.create(address, 32);
        server.createContext("/", this::handle);
        // Java 虚拟线程让健康请求独立调度；业务 semaphore 拒绝过多并发，而不阻塞健康检查。
        server.setExecutor(executor);
    }

    /** Java 进程启动 HTTP 服务，并在容器收到 SIGTERM 时停止接收请求；ADOT hook 刷出剩余 span。 */
    public static void main(String[] args) throws IOException {
        RuntimeServer runtime = new RuntimeServer(new InetSocketAddress("0.0.0.0", 8080),
            (sessionId, prompt) -> new MinimalAgent(sessionId).run(prompt));
        Runtime.getRuntime().addShutdownHook(new Thread(runtime::close, "runtime-shutdown"));
        runtime.start();
        System.out.println(Tools.json(Map.of("event", "runtime_started", "port", 8080)));
    }

    /** Java 服务启动监听；调用者负责在结束时关闭服务。 */
    void start() { server.start(); }

    /** 测试执行者读取操作系统分配的端口，不占用部署使用的固定端口。 */
    int port() { return server.getAddress().getPort(); }

    /** Java 服务只暴露两个精确路径，统一关闭 HTTP exchange，避免请求失败后保留连接资源。 */
    private void handle(HttpExchange exchange) throws IOException {
        try (exchange) {
            String path = exchange.getRequestURI().getPath();
            if ("/ping".equals(path)) {
                if (!requireMethod(exchange, "GET")) return;
                // Java 服务实时报告活跃状态；业务调用占用独立许可，因此不会挤占健康处理线程。
                send(exchange, 200, Map.of("status", "Healthy", "time_of_last_update", Instant.now().getEpochSecond()));
            } else if ("/invocations".equals(path)) {
                if (!requireMethod(exchange, "POST")) return;
                invoke(exchange);
            } else {
                send(exchange, 404, Map.of("error", "not_found"));
            }
        }
    }

    /** Java 服务校验方法并返回 Allow，调用者可以据此修正协议错误。 */
    private boolean requireMethod(HttpExchange exchange, String method) throws IOException {
        if (method.equals(exchange.getRequestMethod())) return true;
        exchange.getResponseHeaders().set("Allow", method);
        send(exchange, 405, Map.of("error", "method_not_allowed"));
        return false;
    }

    /** Java 服务先校验会话、内容类型及有限大小的 JSON，再让业务函数调用模型和工具。 */
    private void invoke(HttpExchange exchange) throws IOException {
        List<String> sessions = exchange.getRequestHeaders().get(SESSION_HEADER);
        if (sessions == null || sessions.size() != 1 || !SESSION_PATTERN.matcher(sessions.getFirst()).matches()) {
            send(exchange, 400, Map.of("error", "invalid_session_id"));
            return;
        }
        String contentType = exchange.getRequestHeaders().getFirst("Content-Type");
        if (contentType == null || !"application/json".equalsIgnoreCase(contentType.split(";", 2)[0].strip())) {
            send(exchange, 415, Map.of("error", "content_type_must_be_application_json"));
            return;
        }
        // Java 服务最多读取上限加一字节，也能拒绝缺少 Content-Length 的过大分块请求。
        byte[] bytes = exchange.getRequestBody().readNBytes(MAX_BODY_BYTES + 1);
        if (bytes.length > MAX_BODY_BYTES) {
            send(exchange, 413, Map.of("error", "request_too_large"));
            return;
        }
        JsonNode body;
        try {
            body = JSON.readTree(bytes);
        } catch (IOException e) {
            send(exchange, 400, Map.of("error", "invalid_json"));
            return;
        }
        JsonNode promptNode = body == null ? null : body.get("prompt");
        if (body == null || !body.isObject() || promptNode == null || !promptNode.isTextual()
                || promptNode.asText().isBlank() || promptNode.asText().length() > MAX_PROMPT_CHARS) {
            send(exchange, 400, Map.of("error", "prompt_must_be_nonempty_text_with_at_most_8192_characters"));
            return;
        }
        if (!invocationSlots.tryAcquire()) {
            send(exchange, 429, Map.of("error", "too_many_concurrent_invocations"));
            return;
        }
        try {
            Map<String, Object> result;
            try {
                result = handler.invoke(sessions.getFirst(), promptNode.asText());
            } catch (Exception e) {
                // Java 服务只输出异常类型；HTTP 错误和容器日志均不包含 SDK 异常中的请求、凭证或内部详情。
                System.err.println(Tools.json(Map.of("event", "runtime_invocation_failed", "error_type", e.getClass().getName())));
                send(exchange, 500, Map.of("error", "agent_invocation_failed"));
                return;
            }
            send(exchange, 200, result);
        } finally {
            invocationSlots.release();
        }
    }

    /** Java 服务统一返回 UTF-8 JSON，客户端不得缓存任务回答或错误。 */
    private static void send(HttpExchange exchange, int status, Map<String, Object> response) throws IOException {
        byte[] bytes = Tools.json(response).getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        exchange.getResponseHeaders().set("Cache-Control", "no-store");
        exchange.sendResponseHeaders(status, bytes.length);
        exchange.getResponseBody().write(bytes);
    }

    /** Java 服务给在途 HTTP 响应短暂退出窗口，随后中断虚拟线程；重复 shutdown 不产生副作用。 */
    @Override
    public void close() {
        if (closed.compareAndSet(false, true)) {
            server.stop(1);
            executor.shutdownNow();
        }
    }
}
