ARG BASE_IMAGE=ghcr.io/anemll/dspark-vllm-gx10@sha256:a83948492cf13df455170fb42885f5ef4db54fefe0feff0f841ecbff464ac9d8
FROM ${BASE_IMAGE}

RUN /usr/bin/python3 -m pip install --no-cache-dir \
      "transformers==4.57.1" \
      "soundfile==0.13.1" \
      "safetensors>=0.4" \
      "tqdm>=4.66" \
    && /usr/bin/python3 -c \
      'import soundfile, torch, transformers; assert transformers.__version__ == "4.57.1"; print(torch.__version__, transformers.__version__, soundfile.__version__)'

WORKDIR /opt/cerebrus-audio8
COPY server.py ./server.py
EXPOSE 8010
ENTRYPOINT ["/usr/bin/python3", "/opt/cerebrus-audio8/server.py"]
