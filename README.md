# 1) 构建镜像
docker build -t openmeteo-agent:1.0 .

# 2) 运行（本机 8080 暴露）
docker run -d --name openmeteo-agent -p 8080:8080 \
  -e AGENT_API_KEY=your-key openmeteo-agent:1.0
