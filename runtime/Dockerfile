# 部署执行者可以传入另一经过审查的 ARM64 Java 21 镜像；默认固定多架构 manifest digest。
ARG JAVA_BASE_IMAGE=public.ecr.aws/docker/library/eclipse-temurin:21-jre-noble@sha256:000fd431958bc81a24abe1e8e5f0f0fd3ae365a594bd50aadb20696805f9408c
FROM ${JAVA_BASE_IMAGE}
WORKDIR /app
# 镜像构建器创建专用非 root 用户；Java 与 ADOT 的临时文件仅写入应用目录。
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin agent \
    && mkdir -p /app/tmp && chown 10001:10001 /app/tmp
COPY --chown=10001:10001 app.jar aws-opentelemetry-agent.jar /app/
COPY --chown=10001:10001 entrypoint.sh /app/entrypoint.sh
RUN chmod 0555 /app/entrypoint.sh
ENV TMPDIR=/app/tmp
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["/app/entrypoint.sh"]
