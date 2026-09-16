package example.agentcore;

import static org.junit.jupiter.api.Assertions.*;
import java.math.BigDecimal;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import software.amazon.awssdk.services.bedrockruntime.model.*;

/**
 * JUnit 在本地校验 Java 工具执行及遥测转换契约。测试仅构造 AWS SDK 消息对象，不创建客户端或调用模型。
 * 本测试类不启动 ADOT provider；ADOT 云端导出和 Eval 评分需要测试执行者另行开展集成验证。
 */
class AgentContractTest {
    /** JUnit 验证 Java calculator 的十进制乘法，并确认工具层拒绝除零、未知订单和字符串数字。 */
    @Test void decimalArithmeticAndUnknownOrders() throws Exception {
        var result = Tools.execute("calculator", Tools.JSON.readTree("{\"operation\":\"multiply\",\"a\":24.50,\"b\":3}"));
        // 工具层会去除尾零；JUnit 比较数值而非 BigDecimal 的 scale，避免把 73.5 与 73.50 判为不同金额。
        assertEquals(0, new BigDecimal("73.50").compareTo((BigDecimal) result.get("result")));
        // JUnit 确认错误仍由 Java 工具层抛出，供编排层包装为模型能够读取的 ERROR toolResult。
        assertThrows(ArithmeticException.class, () -> Tools.execute("calculator", Tools.JSON.readTree("{\"operation\":\"divide\",\"a\":1,\"b\":0}")));
        assertThrows(IllegalArgumentException.class, () -> Tools.execute("order_lookup", Tools.JSON.readTree("{\"order_id\":\"missing\"}")));
        assertThrows(IllegalArgumentException.class, () -> Tools.execute("calculator", Tools.JSON.readTree("{\"operation\":\"add\",\"a\":\"1\",\"b\":2}")));
    }

    /** JUnit 验证 Java 转换器保留工具调用 ID，并将 Bedrock 的 USER 工具结果映射为 GenAI 的 tool 角色。 */
    @Test void telemetryPreservesToolCallIdAndUsesToolRole() {
        // JUnit 用本地 SDK 对象模拟协议输入；这些固定消息不冒充真实 Bedrock 推理或线上 Eval 结果。
        var call = ToolUseBlock.builder().toolUseId("call-17").name("calculator")
            .input(Tools.document(Map.of("operation", "multiply", "a", 24.5, "b", 3))).build();
        var result = ToolResultBlock.builder().toolUseId("call-17").status(ToolResultStatus.SUCCESS)
            .content(ToolResultContentBlock.fromJson(Tools.document(Map.of("result", 73.5)))).build();
        // Java 转换器只修改遥测表示；实际 Converse 消息仍按 Bedrock 协议使用 ASSISTANT / USER 角色。
        var messages = Tools.JSON.valueToTree(MinimalAgent.messages(List.of(
            Message.builder().role(ConversationRole.ASSISTANT).content(ContentBlock.fromToolUse(call)).build(),
            Message.builder().role(ConversationRole.USER).content(ContentBlock.fromToolResult(result)).build())));
        // JUnit 检查 Eval 推理消息字段所使用的 GenAI 结构，防止请求与结果失去关联或丢失工具输出。
        assertEquals("tool", messages.get(1).path("role").asText());
        assertEquals("tool_call", messages.get(0).at("/parts/0/type").asText());
        assertEquals("tool_call_response", messages.get(1).at("/parts/0/type").asText());
        assertEquals(messages.get(0).at("/parts/0/id"), messages.get(1).at("/parts/0/id"));
        assertEquals(73.5, messages.get(1).at("/parts/0/response/result").asDouble());
    }
}
