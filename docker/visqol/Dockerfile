# ViSQOL audio-mode scorer for Mediaforce's audio quality check.
# ViSQOL does not build with the macOS toolchain, so Mediaforce runs it here.
# Build: docker build -t mediaforce-visqol:38d0b01 docker/visqol

ARG BASE=ubuntu:22.04@sha256:5ec03bb3441e8b0bf3b4f9cd4629a1ae763010dc3035bb8da3ae6cf026486401

FROM ${BASE} AS build
ARG VISQOL_COMMIT=38d0b0163e441047d4429bf07ad09e5b9031d02c
ARG BAZEL_VERSION=5.3.2
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       build-essential ca-certificates curl git python3 python3-dev python3-numpy unzip zip \
    && rm -rf /var/lib/apt/lists/*
RUN arch="$(dpkg --print-architecture | sed 's/amd64/x86_64/')" \
    && curl -fsSL -o /usr/local/bin/bazel \
       "https://github.com/bazelbuild/bazel/releases/download/${BAZEL_VERSION}/bazel-${BAZEL_VERSION}-linux-${arch}" \
    && chmod +x /usr/local/bin/bazel
RUN git init /visqol && cd /visqol \
    && git fetch --depth 1 https://github.com/google/visqol "${VISQOL_COMMIT}" \
    && git checkout FETCH_HEAD
WORKDIR /visqol
RUN bazel build :visqol -c opt && cp bazel-bin/visqol /usr/local/bin/visqol

FROM ${BASE}
COPY --from=build /usr/local/bin/visqol /usr/local/bin/visqol
COPY --from=build /visqol/model /visqol/model
WORKDIR /visqol
ENTRYPOINT ["/usr/local/bin/visqol"]
