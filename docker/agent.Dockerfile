ARG RUST_VERSION=1.83
FROM rust:${RUST_VERSION}-slim-bookworm

ARG OPENCODE_VERSION=2.2.5
ENV DEBIAN_FRONTEND=noninteractive
ENV HOME=/home/agent

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl git ripgrep procps unzip build-essential pkg-config python3 \
 && rm -rf /var/lib/apt/lists/*

# OpenCode 2. The v1 installer refuses 2.x, so use the v2 endpoint.
RUN mkdir -p /home/agent \
 && curl -fsSL https://opencode.ai/v2/install \
      | HOME=/home/agent bash -s -- --version "${OPENCODE_VERSION}" --no-modify-path \
 && /home/agent/.opencode/bin/opencode --version
ENV PATH=/home/agent/.opencode/bin:$PATH

RUN mkdir -p /home/agent/.local/share/opencode /home/agent/.cache /work
COPY docker/entrypoint.sh /usr/local/bin/opencode-bench-entrypoint
RUN chmod +x /usr/local/bin/opencode-bench-entrypoint

WORKDIR /work
ENTRYPOINT ["/usr/local/bin/opencode-bench-entrypoint"]
CMD ["bash"]
