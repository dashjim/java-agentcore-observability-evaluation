package example.agentcore;

import static org.junit.jupiter.api.Assertions.*;

import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

/** 测试执行者使用真实本地 HTTP 和假业务函数验证 Runtime 协议，全程不创建 AWS 客户端。 */
class RuntimeServerTest {
    private static final String SESSION_A = "java-runtime-test-session-000000001";
    private static final String SESSION_B = "java-runtime-test-session-000000002";
    private final HttpClient client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build();
    private RuntimeServer server;

    /** 测试执行者为每个测试启动独立端口与业务函数，防止状态在测试间串扰。 */
    private void start(RuntimeServer.InvocationHandler handler) throws Exception {
        server = new RuntimeServer(new InetSocketAddress("127.0.0.1", 0), handler);
        server.start();
    }

    /** 测试执行者关闭 HTTP 连接池和服务线程，验证服务支持容器终止时的资源回收。 */
    @AfterEach
    void stop() {
        if (server != null) server.close();
        client.close();
    }

    /** 测试客户端构造请求，调用者可以省略会话头以覆盖错误分支。 */
    private HttpRequest request(String method, String path, String body, String session) {
        var builder = HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + server.port() + path))
            .timeout(Duration.ofSeconds(5)).header("Content-Type", "application/json");
        if (session != null) builder.header(RuntimeServer.SESSION_HEADER, session);
        return builder.method(method, HttpRequest.BodyPublishers.ofString(body)).build();
    }

    /** 测试客户端返回完整字符串响应，以检查 HTTP 状态和 JSON 协议。 */
    private HttpResponse<String> send(HttpRequest request) throws Exception {
        return client.send(request, HttpResponse.BodyHandlers.ofString());
    }

    /** 测试执行者验证健康 JSON、方法提示和精确路径，避免前缀路径意外暴露入口。 */
    @Test
    void healthAndRoutingFollowRuntimeContract() throws Exception {
        start((session, prompt) -> Map.of());
        var ping = send(request("GET", "/ping", "", null));
        assertEquals(200, ping.statusCode());
        var body = Tools.JSON.readTree(ping.body());
        assertEquals("Healthy", body.path("status").asText());
        assertTrue(Math.abs(Instant.now().getEpochSecond() - body.path("time_of_last_update").asLong()) < 5);
        assertTrue(ping.headers().firstValue("Content-Type").orElseThrow().startsWith("application/json"));
        var wrongMethod = send(request("GET", "/invocations", "", null));
        assertEquals(405, wrongMethod.statusCode());
        assertEquals("POST", wrongMethod.headers().firstValue("Allow").orElseThrow());
        assertEquals(404, send(request("GET", "/ping/extra", "", null)).statusCode());
    }

    /** 测试执行者让两次业务调用重叠，检查 HTTP 服务未混用会话和任务。 */
    @Test
    void concurrentRequestsKeepTheirOwnSessionAndPrompt() throws Exception {
        CountDownLatch entered = new CountDownLatch(2);
        start((session, prompt) -> {
            entered.countDown();
            assertTrue(entered.await(3, TimeUnit.SECONDS));
            return Map.of("session_id", session, "answer", prompt, "successful_tools", List.of("order_lookup", "calculator"));
        });
        var first = client.sendAsync(request("POST", "/invocations", "{\"prompt\":\"第一个订单\"}", SESSION_A), HttpResponse.BodyHandlers.ofString());
        var second = client.sendAsync(request("POST", "/invocations", "{\"prompt\":\"第二个订单\"}", SESSION_B), HttpResponse.BodyHandlers.ofString());
        var responseA = first.get(5, TimeUnit.SECONDS);
        var responseB = second.get(5, TimeUnit.SECONDS);
        assertEquals(200, responseA.statusCode());
        assertEquals(200, responseB.statusCode());
        assertEquals(SESSION_A, Tools.JSON.readTree(responseA.body()).path("session_id").asText());
        assertEquals("第一个订单", Tools.JSON.readTree(responseA.body()).path("answer").asText());
        assertEquals(SESSION_B, Tools.JSON.readTree(responseB.body()).path("session_id").asText());
        assertEquals("第二个订单", Tools.JSON.readTree(responseB.body()).path("answer").asText());
    }

    /** 测试执行者确认协议拒绝不明确的会话，假业务函数不应收到这些请求。 */
    @Test
    void rejectsMissingInvalidAndDuplicateSessionsBeforeBusinessExecution() throws Exception {
        AtomicInteger calls = new AtomicInteger();
        start((session, prompt) -> { calls.incrementAndGet(); return Map.of(); });
        for (String session : new String[] {null, "short", "x".repeat(257), "!" + "x".repeat(32)}) {
            assertEquals(400, send(request("POST", "/invocations", "{\"prompt\":\"test\"}", session)).statusCode());
        }
        var duplicate = HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + server.port() + "/invocations"))
            .header("Content-Type", "application/json").header(RuntimeServer.SESSION_HEADER, SESSION_A)
            .header(RuntimeServer.SESSION_HEADER, SESSION_B)
            .POST(HttpRequest.BodyPublishers.ofString("{\"prompt\":\"test\"}")).build();
        assertEquals(400, send(duplicate).statusCode());
        assertEquals(0, calls.get());
    }

    /** 测试执行者覆盖 JSON 歧义和输入大小边界，避免无效任务触发收费模型调用。 */
    @Test
    void rejectsMalformedAmbiguousAndOversizedPayloadsBeforeBusinessExecution() throws Exception {
        AtomicInteger calls = new AtomicInteger();
        start((session, prompt) -> { calls.incrementAndGet(); return Map.of(); });
        for (String body : List.of("", "{", "[]", "null", "{}", "{\"prompt\":1}", "{\"prompt\":\"  \"}",
                "{\"prompt\":\"a\",\"prompt\":\"b\"}", "{\"prompt\":\"a\"} {}",
                Tools.json(Map.of("prompt", "a".repeat(RuntimeServer.MAX_PROMPT_CHARS + 1))))) {
            assertEquals(400, send(request("POST", "/invocations", body, SESSION_A)).statusCode(), body.substring(0, Math.min(body.length(), 50)));
        }
        assertEquals(413, send(request("POST", "/invocations", "a".repeat(RuntimeServer.MAX_BODY_BYTES + 1), SESSION_A)).statusCode());
        var wrongType = HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + server.port() + "/invocations"))
            .header("Content-Type", "text/plain").header(RuntimeServer.SESSION_HEADER, SESSION_A)
            .POST(HttpRequest.BodyPublishers.ofString("{\"prompt\":\"test\"}")).build();
        assertEquals(415, send(wrongType).statusCode());
        assertEquals(0, calls.get());
    }

    /** 测试执行者占满业务许可，验证服务继续响应健康请求并拒绝超额业务并发。 */
    @Test
    void healthyEndpointRespondsWhenAllBusinessSlotsAreOccupied() throws Exception {
        CountDownLatch entered = new CountDownLatch(4);
        CountDownLatch release = new CountDownLatch(1);
        start((session, prompt) -> {
            entered.countDown();
            assertTrue(release.await(4, TimeUnit.SECONDS));
            return Map.of("session_id", session);
        });
        List<CompletableFuture<HttpResponse<String>>> requests = new ArrayList<>();
        try {
            for (int i = 0; i < 4; i++) requests.add(client.sendAsync(request("POST", "/invocations", "{\"prompt\":\"test\"}", SESSION_A), HttpResponse.BodyHandlers.ofString()));
            assertTrue(entered.await(3, TimeUnit.SECONDS));
            assertEquals(429, send(request("POST", "/invocations", "{\"prompt\":\"test\"}", SESSION_B)).statusCode());
            assertEquals(200, send(request("GET", "/ping", "", null)).statusCode());
        } finally {
            release.countDown();
        }
        for (var pending : requests) assertEquals(200, pending.get(5, TimeUnit.SECONDS).statusCode());
    }

    /** 测试执行者注入含敏感示例文本的异常，验证客户端只收到通用错误且后续请求可成功。 */
    @Test
    void businessFailureDoesNotExposeExceptionAndReleasesInvocationSlot() throws Exception {
        AtomicInteger calls = new AtomicInteger();
        start((session, prompt) -> {
            if (calls.getAndIncrement() == 0) throw new IllegalStateException("example-secret-in-internal-error");
            return Map.of("answer", "recovered", "session_id", session);
        });
        var failed = send(request("POST", "/invocations", "{\"prompt\":\"test\"}", SESSION_A));
        assertEquals(500, failed.statusCode());
        assertEquals("agent_invocation_failed", Tools.JSON.readTree(failed.body()).path("error").asText());
        assertFalse(failed.body().contains("example-secret"));
        assertEquals(200, send(request("POST", "/invocations", "{\"prompt\":\"test\"}", SESSION_B)).statusCode());
    }
}
