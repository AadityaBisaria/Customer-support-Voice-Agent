FROM dailyco/pipecat-base:latest

# Enable bytecode compilation
ENV UV_COMPILE_BYTECODE=1

# Copy from the cache instead of linking since it's a mounted volume
ENV UV_LINK_MODE=copy

# Uncomment if you wish to print a summary of the features available in the base image
# ENV PCC_LOG_FEATURES_SUMMARY=true

# Install the project's dependencies using the lockfile and settings
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev

# Copy the application code. Anything not copied here silently vanishes in
# the deployed image — every package bot.py imports must be listed.
COPY ./bot.py bot.py
COPY ./language language
COPY ./rag rag
COPY ./dialogue dialogue
COPY ./store store
COPY ./flows flows

# Pre-bake the RAG embedding model (~220MB) so cold starts don't download it.
# The cache path is pinned so the runtime finds what this RUN downloaded.
ENV FASTEMBED_CACHE_PATH=/app/.fastembed
RUN uv run --no-sync python -c "from fastembed import TextEmbedding; TextEmbedding('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')"