FROM golang:latest as builder

WORKDIR /

# install python libs and scripts and generate initial feed

RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
    curl \
    python3-full \
    python3-setuptools \
    python3-pip \
    rsync \
    && rm -rf /var/lib/apt/lists/*
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN mkdir -p /pkg/deals
COPY pyproject.toml /pkg/deals/
COPY src /pkg/deals/
COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv /venv
RUN set -ex && \
    /venv/bin/python -m pip install --upgrade pip && \
    /venv/bin/python -m pip install -r /tmp/requirements.txt && \
    /venv/bin/python -m pip install /pkg/deals && \
    rm -rf /root/.cache/

COPY wantlist.pickle /
COPY searches.pickle /
COPY index.xml /

RUN mkdir -p /srv/http
COPY wantlist.pickle /srv/http/
COPY searches.pickle /srv/http/
COPY index.xml /srv/http/

COPY update-feed.sh /
COPY update-wantlist.sh /
COPY entrypoint.sh /

# install supercronic and crontab

RUN go install github.com/aptible/supercronic@latest
COPY crontab /
RUN supercronic -test ./crontab

# install goStatic

RUN go install github.com/PierreZ/goStatic@latest

# start supercronic and goStatic

ENTRYPOINT ["./entrypoint.sh"]
