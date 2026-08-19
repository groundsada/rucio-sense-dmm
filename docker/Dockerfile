# Installs the published sense-dmm from PyPI, not this tree — it will not carry
# anything unreleased. Build the Dockerfile at the repo root to test a change.
FROM python:3.10-slim-bullseye

RUN apt update && apt install sqlite3
RUN apt-get clean autoclean && \
    apt-get autoremove --yes && \
    rm -rf /var/lib/{apt,dpkg,cache,log}/

RUN pip3 install sense-dmm

COPY ./docker/wait-for-it.sh /wait-for-it.sh
COPY ./docker/docker-entrypoint.sh /docker-entrypoint.sh

ENV PYTHONPATH=/opt/dmm/
ENV DMM_CONFIG /opt/dmm/dmm.cfg

EXPOSE 80

ENTRYPOINT ["/docker-entrypoint.sh"]
