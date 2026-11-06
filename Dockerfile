FROM python:3.14-slim
RUN pip install --no-cache-dir uv==0.11.32
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev
ENV UPPYT_HOME=/home/uppyt
RUN useradd --create-home uppyt && chown -R uppyt /app
USER uppyt
EXPOSE 8770
CMD ["/app/.venv/bin/uppyt", "--host", "0.0.0.0", "--no-browser"]
