ARG RUST_VERSION=1.83
FROM rust:${RUST_VERSION}-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl git ripgrep procps unzip build-essential cmake python3 \
 && rm -rf /var/lib/apt/lists/*

# The unprivileged account used for candidate and baseline codec processes.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin bench

# Independent, pinned OpenJPH oracle. This is the only correctness reference;
# it is mode 700 so the uid-1000 candidate process cannot read it.
COPY tasks/htj2k/tests/oracle/OpenJPH /opt/src/OpenJPH
COPY tasks/htj2k/tests/oracle/oracle.cpp /opt/src/oracle.cpp
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

ENV HOME=/root
WORKDIR /opt/tests
