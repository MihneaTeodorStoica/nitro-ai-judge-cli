FROM docker:27-cli AS docker-cli

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose

WORKDIR /opt/naij
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip install ".[manager]" \
    && docker compose version \
    && mkdir -p /var/lib/naij/projects \
    && chmod 0700 /var/lib/naij /var/lib/naij/projects

EXPOSE 51123
VOLUME ["/var/lib/naij"]
ENTRYPOINT ["python", "-m", "nitro_ai_judge_cli.manager"]
