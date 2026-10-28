FROM ubuntu:24.04
RUN apt-get update && apt-get install -y git python3 curl bash nodejs npm procps
# Pre-install claude-code during build (image-cached) so per-trial agent
# setup is fast and doesn't re-download the binary.
RUN curl -fsSL https://downloads.claude.ai/claude-code-releases/bootstrap.sh | bash -s -- \
 && echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
ENV PATH="/root/.local/bin:${PATH}"
WORKDIR /app
