# syntax=docker/dockerfile:1.4
# Build with ./bench.py build-eval --tests-dir PRIVATE_SUITE_DIR.
# The named `tests` build context deliberately comes from the private evaluator
# repository so neither the public repository nor the public agent image ever
# contains evaluation sources.
ARG RUST_VERSION=1.83
FROM rust:${RUST_VERSION}-slim-bookworm

ARG EVALUATOR_SUITE_VERSION=local
ARG EVALUATOR_SOURCE_REVISION=local

ENV DEBIAN_FRONTEND=noninteractive \
    EVALUATOR_SUITE=htj2k \
    EVALUATOR_SUITE_VERSION=${EVALUATOR_SUITE_VERSION} \
    EVALUATOR_SOURCE_REVISION=${EVALUATOR_SOURCE_REVISION}

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl git ripgrep procps unzip build-essential cmake python3 \
 && rm -rf /var/lib/apt/lists/*

# The unprivileged account used for candidate and baseline codec processes.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin bench

# Independent, pinned OpenJPH oracle. This is the only correctness reference;
# it is mode 700 so the uid-1000 candidate process cannot read it.
COPY --from=tests oracle/OpenJPH /opt/src/OpenJPH
COPY --from=tests oracle/oracle.cpp /opt/src/oracle.cpp
RUN cmake -S /opt/src/OpenJPH -B /opt/build \
      -DCMAKE_BUILD_TYPE=Release \
      -DBUILD_SHARED_LIBS=OFF \
      -DOJPH_ENABLE_TIFF_SUPPORT=OFF \
      -DOJPH_BUILD_TESTS=OFF \
      -DOJPH_BUILD_EXECUTABLES=OFF \
 && cmake --build /opt/build -j"$(nproc)" \
 && mkdir -p /opt/oracle/bin \
 && c++ -O3 -std=c++17 \
      -I/opt/src/OpenJPH/src/core/openjph \
      /opt/src/oracle.cpp \
      /opt/build/src/core/libopenjph.a \
      -o /opt/oracle/bin/oracle \
 && chmod -R 700 /opt/oracle /opt/src /opt/build

# Hidden suite: runner, corpus generator, frozen manifest and fixtures.
# Mode 700 so the uid-1000 candidate process cannot read it.
COPY --from=tests runner.py corpus.py manifest.json /opt/private/
COPY --from=tests fixtures /opt/private/fixtures
RUN chmod -R 700 /opt/private \
 && python3 -B -c "import json; m = json.load(open('/opt/private/manifest.json')); assert m.get('suite_version'), 'manifest missing suite_version'"

LABEL org.opencode-bench.suite="htj2k" \
      org.opencode-bench.evaluator-suite-version="${EVALUATOR_SUITE_VERSION}" \
      org.opencode-bench.evaluator-source-revision="${EVALUATOR_SOURCE_REVISION}"

ENV HOME=/root
WORKDIR /opt/tests
