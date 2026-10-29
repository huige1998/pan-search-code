FROM python:3.12-slim
WORKDIR /app
COPY workspace /app/workspace
COPY requirements.txt /app/requirements.txt
COPY entrypoint.sh /app/entrypoint.sh
RUN pip install --no-cache-dir --target /app/workspace/vendor/pylibs -r /app/requirements.txt \
    && chmod +x /app/entrypoint.sh
ENV HOME=/app TZ=Asia/Shanghai
EXPOSE 7860
CMD ["/app/entrypoint.sh"]
