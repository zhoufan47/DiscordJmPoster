# 使用官方 Python 轻量级镜像
FROM python:3.9-slim

# 设置工作目录
WORKDIR /app

# 设置时区为上海 (可选，方便看日志)
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 设置基础参数配置

# 复制依赖文件并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制源代码
COPY main.py .
# 如果有 config.json 也可以复制，但我们推荐用环境变量
# COPY config.json .

# 创建必要的目录
RUN mkdir -p logs


# 暴露端口
EXPOSE 8000

# 启动命令
CMD ["python", "main.py"]