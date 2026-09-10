FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "mcp<2" https://github.com/HumanSignal/label-studio-mcp-server/archive/9505071cbe28bf3f4af47ad1ea55e2929ace59fc.tar.gz
COPY launch.py /app/launch.py
ENTRYPOINT ["python", "/app/launch.py"]
