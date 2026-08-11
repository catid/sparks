ARG BASE_IMAGE=ghcr.io/anemll/dspark-vllm-gx10@sha256:a83948492cf13df455170fb42885f5ef4db54fefe0feff0f841ecbff464ac9d8
FROM ${BASE_IMAGE}

RUN /usr/bin/python3 -m pip install --no-cache-dir \
      "transformers==5.13.0" \
      "safetensors>=0.4,<1" \
    && /usr/bin/python3 -c \
      'import torch, transformers; from transformers import AutoModelForMultimodalLM; assert transformers.__version__ == "5.13.0"; print(torch.__version__, transformers.__version__)'

WORKDIR /opt/cerebrus-qwen-asr
COPY asr_server.py ./asr_server.py
EXPOSE 8020
ENTRYPOINT ["/usr/bin/python3", "/opt/cerebrus-qwen-asr/asr_server.py"]
