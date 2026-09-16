package example.agentcore;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.math.BigDecimal;
import java.math.MathContext;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import software.amazon.awssdk.core.document.Document;
import software.amazon.awssdk.services.bedrockruntime.model.Tool;
import software.amazon.awssdk.services.bedrockruntime.model.ToolConfiguration;
import software.amazon.awssdk.services.bedrockruntime.model.ToolInputSchema;
import software.amazon.awssdk.services.bedrockruntime.model.ToolSpecification;

/**
 * Java 应用提供的本地工具及 JSON 转换器。order_lookup 读取固定订单 fixture，calculator 使用十进制运算。
 * Bedrock 模型只选择工具和参数；本类负责校验与执行，不调用 AWS，也不生成模型回答或 Eval 评分。
 */
final class Tools {
    // Java 工具层复用默认 JSON 映射器；本类只处理 JSON 树、集合及基本值，不启用多态类型反序列化。
    static final ObjectMapper JSON = new ObjectMapper();
    // Java 应用复用同一组定义构建 Bedrock 工具 schema 和遥测属性，避免模型与观测侧看到不同的接口。
    static final List<Map<String, Object>> DEFINITIONS = List.of(
        Map.of("type", "function", "name", "order_lookup", "description",
            "Look up a demo order in the local fixture by order_id. Returns unit_price, quantity, currency and status.",
            "parameters", Map.of("type", "object", "properties", Map.of("order_id", Map.of("type", "string")),
                "required", List.of("order_id"), "additionalProperties", false)),
        Map.of("type", "function", "name", "calculator", "description",
            "Calculate add, subtract, multiply or divide on two decimal numbers. Use this tool for arithmetic.",
            "parameters", Map.of("type", "object", "properties", Map.of(
                "operation", Map.of("type", "string", "enum", List.of("add", "subtract", "multiply", "divide")),
                "a", Map.of("type", "number"), "b", Map.of("type", "number")),
                "required", List.of("operation", "a", "b"), "additionalProperties", false)));

    /**
     * Java 工具层将本地工具定义转换为 Bedrock Converse 的工具配置，不触发模型调用。
     *
     * @return Java 应用随推理请求提交的工具名称、说明和输入 schema
     */
    static ToolConfiguration configuration() {
        return ToolConfiguration.builder().tools(DEFINITIONS.stream().map(d -> Tool.builder()
            .toolSpec(ToolSpecification.builder().name((String) d.get("name"))
                .description((String) d.get("description"))
                .inputSchema(ToolInputSchema.builder().json(document(d.get("parameters"))).build()).build())
            .build()).toList()).build();
    }

    /**
     * Java 工具层按模型给出的名称分派工具，并独立校验输入，防止仅依赖模型遵守 schema。
     *
     * @param name Bedrock 模型请求的工具名称
     * @param args Java 编排层从模型输入转换出的 JSON 参数对象
     * @return Java 工具实际执行后得到的订单字段或计算结果
     * @throws IllegalArgumentException 工具未知、参数不符合约定，或订单不在 fixture 中
     * @throws ArithmeticException calculator 执行除零等无效运算
     */
    static Map<String, Object> execute(String name, JsonNode args) {
        if (!args.isObject()) throw new IllegalArgumentException("Tool arguments must be an object");
        return switch (name) {
            case "order_lookup" -> {
                requireFields(args, Set.of("order_id"));
                if (!args.path("order_id").isTextual()) throw new IllegalArgumentException("order_id must be a string");
                if (!"ORD-1001".equals(args.path("order_id").asText()))
                    throw new IllegalArgumentException("Order not found in local demo fixture");
                // Java 工具仅返回这一条演示订单，不查询外部订单系统；source 明确告知模型数据来自 fixture。
                yield Map.of("order_id", "ORD-1001", "status", "SHIPPED", "unit_price", new BigDecimal("24.50"),
                    "quantity", 3, "currency", "USD", "source", "local_demo_fixture");
            }
            case "calculator" -> {
                requireFields(args, Set.of("operation", "a", "b"));
                if (!args.path("a").isNumber() || !args.path("b").isNumber())
                    throw new IllegalArgumentException("a and b must be JSON numbers");
                // Java calculator 用 BigDecimal 计算；除法按 DECIMAL128 舍入，避免无限小数无法返回结果。
                BigDecimal a = args.get("a").decimalValue(), b = args.get("b").decimalValue();
                BigDecimal result = switch (args.path("operation").asText()) {
                    case "add" -> a.add(b);
                    case "subtract" -> a.subtract(b);
                    case "multiply" -> a.multiply(b);
                    case "divide" -> a.divide(b, MathContext.DECIMAL128);
                    default -> throw new IllegalArgumentException("Unsupported calculator operation");
                };
                // Java 工具去除无意义的尾零，但保留数值；最终货币表述由 Bedrock 模型根据订单字段生成。
                yield Map.of("result", result.stripTrailingZeros());
            }
            default -> throw new IllegalArgumentException("Unknown tool: " + name);
        };
    }

    /**
     * Java 工具层落实 schema 的必填字段及禁止额外字段约束，拒绝字段缺失、null 或字段数不符。
     *
     * @param args 已确认属于对象类型的 JSON 参数
     * @param fields 当前工具允许且必须提供的字段集合
     */
    private static void requireFields(JsonNode args, Set<String> fields) {
        if (args.size() != fields.size() || fields.stream().anyMatch(f -> !args.hasNonNull(f)))
            throw new IllegalArgumentException("Tool arguments must contain exactly " + fields);
    }

    /**
     * Java 工具层将对象序列化为 JSON，供应用输出结构化事件或写入遥测内容属性。
     *
     * @param value JSON 映射器支持的值
     * @return JSON 字符串；ADOT 导出 span 属性，Eval 按约定属性读取其中的内容
     * @throws IllegalArgumentException JSON 映射器无法序列化该值
     */
    static String json(Object value) {
        try { return JSON.writeValueAsString(value); }
        catch (JsonProcessingException e) { throw new IllegalArgumentException("Cannot serialize telemetry", e); }
    }

    /**
     * Java 工具层经 JSON 树将 schema 或工具结果转换成 AWS SDK Document。
     *
     * @param value Java 集合、基本值或其他可映射为 JSON 的值
     * @return Bedrock Converse 请求所需的 Document，不涉及网络调用
     */
    static Document document(Object value) { return documentNode(JSON.valueToTree(value)); }

    /**
     * Java 转换器递归映射 JSON 节点；数字经 BigDecimal 传递，避免此转换步骤引入 double 舍入。
     *
     * @param node JSON 映射器创建的值节点、数组或对象
     * @return 与输入 JSON 结构对应的 AWS SDK Document
     */
    private static Document documentNode(JsonNode node) {
        if (node.isNull()) return Document.fromNull();
        if (node.isTextual()) return Document.fromString(node.asText());
        if (node.isBoolean()) return Document.fromBoolean(node.asBoolean());
        if (node.isNumber()) return Document.fromNumber(node.decimalValue());
        if (node.isArray()) {
            var values = new java.util.ArrayList<Document>();
            node.forEach(n -> values.add(documentNode(n)));
            return Document.fromList(values);
        }
        var values = new LinkedHashMap<String, Document>();
        node.fields().forEachRemaining(e -> values.put(e.getKey(), documentNode(e.getValue())));
        return Document.fromMap(values);
    }

    /**
     * Java 转换器递归展开模型输入或工具结果中的 Document，供参数校验和 GenAI 消息序列化使用。
     *
     * @param d AWS SDK 表示的 JSON 值
     * @return Java 基本值、BigDecimal、列表或映射；Document null 对应 Java null
     */
    static Object value(Document d) {
        if (d.isNull()) return null;
        if (d.isString()) return d.asString();
        if (d.isBoolean()) return d.asBoolean();
        if (d.isNumber()) return d.asNumber().bigDecimalValue();
        if (d.isList()) return d.asList().stream().map(Tools::value).toList();
        var result = new LinkedHashMap<String, Object>();
        d.asMap().forEach((k, v) -> result.put(k, value(v)));
        return result;
    }
}
