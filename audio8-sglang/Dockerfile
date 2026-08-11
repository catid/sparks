ARG BASE_IMAGE=lmsysorg/sglang@sha256:032ac5457fdff735f21abc366aed0f76ddb699d51fdbe17745c2a0e53579b6ea
FROM ${BASE_IMAGE}

ARG SGLANG_OMNI_COMMIT=68a572348837f7b004857b4b07993c20ade4c017
ARG AUDIO8_TTS_COMMIT=9393162327a0cfb7045f55652665bcc93c9be54f

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates git libnuma1 \
    && apt-get clean \
    && find /var/lib/apt/lists -mindepth 1 -delete

COPY patches/sglang-omni-audio8-only.patch /tmp/sglang-omni-audio8-only.patch
COPY patches/sglang-omni-audio8-health.patch /tmp/sglang-omni-audio8-health.patch
COPY patches/audio8-pinned-omni-compat.patch /tmp/audio8-pinned-omni-compat.patch
COPY patches/audio8-fixed-reference.patch /tmp/audio8-fixed-reference.patch
COPY RUNTIME.lock.json /tmp/audio8-sglang-runtime.lock.json
COPY verify_source_contract.py /tmp/verify-audio8-source-contract.py

RUN git clone --filter=blob:none https://github.com/sgl-project/sglang-omni.git /opt/sglang-omni \
    && git -C /opt/sglang-omni checkout --detach "${SGLANG_OMNI_COMMIT}" \
    && test "$(git -C /opt/sglang-omni rev-parse HEAD)" = "${SGLANG_OMNI_COMMIT}" \
    && git -C /opt/sglang-omni apply --check /tmp/sglang-omni-audio8-only.patch \
    && git -C /opt/sglang-omni apply /tmp/sglang-omni-audio8-only.patch \
    && git -C /opt/sglang-omni apply --check /tmp/sglang-omni-audio8-health.patch \
    && git -C /opt/sglang-omni apply /tmp/sglang-omni-audio8-health.patch \
    && python3 -m pip install --no-cache-dir --no-deps --editable /opt/sglang-omni \
    && python3 -m pip install --no-cache-dir --no-deps \
        msgpack==1.2.1 shellingham==1.5.4 typer==0.27.1

RUN git clone --filter=blob:none https://github.com/Audio8-AI/Audio8_TTS.git /opt/audio8-tts \
    && git -C /opt/audio8-tts checkout --detach "${AUDIO8_TTS_COMMIT}" \
    && test "$(git -C /opt/audio8-tts rev-parse HEAD)" = "${AUDIO8_TTS_COMMIT}" \
    && python3 /tmp/verify-audio8-source-contract.py \
        /tmp/audio8-sglang-runtime.lock.json /opt/sglang-omni /opt/audio8-tts \
    && git -C /opt/audio8-tts apply --check /tmp/audio8-pinned-omni-compat.patch \
    && git -C /opt/audio8-tts apply /tmp/audio8-pinned-omni-compat.patch \
    && git -C /opt/audio8-tts apply --check /tmp/audio8-fixed-reference.patch \
    && git -C /opt/audio8-tts apply /tmp/audio8-fixed-reference.patch \
    && /opt/audio8-tts/sglang_omni/scripts/install_adapter.sh /opt/sglang-omni \
    && python3 -m compileall -q /opt/sglang-omni/sglang_omni/models/audio8_tts

ENV CUDA_PATH=/usr/local/cuda \
    FLASHINFER_WORKSPACE_BASE=/cache/flashinfer \
    SGLANG_OMNI_AUDIO8_ONLY=1 \
    TORCHINDUCTOR_CACHE_DIR=/cache/torchinductor \
    TRITON_CACHE_DIR=/cache/triton

WORKDIR /opt/audio8-tts
