FROM golang:latest as builder

WORKDIR /

# install python libs and scripts and generate initial feed

RUN apt-get update && apt-get install -y \
    curl \
    python3 \
    python3-setuptools \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
COPY requirements.txt /tmp/requirements.txt
RUN set -ex && \
    python3 -m pip install --upgrade pip && \
    python3 -m pip install -r /tmp/requirements.txt && \
    rm -rf /root/.cache/
RUN mkdir -p /srv/http
RUN curl -o /srv/http/index.xml https://deals.fly.dev/index.xml
COPY deals.py /
COPY config.py /
COPY criteria.py /
COPY update-feed.sh /
RUN --mount=type=secret,id=DISCOGS_USER \
    --mount=type=secret,id=TOKEN \
    --mount=type=secret,id=FEED_URL \
    --mount=type=secret,id=FEED_AUTHOR_NAME \
    --mount=type=secret,id=FEED_AUTHOR_EMAIL \
    DISCOGS_USER="$(cat /run/secrets/DISCOGS_USER)" \
    TOKEN="$(cat /run/secrets/TOKEN)" \
    FEED_URL="$(cat /run/secrets/FEED_URL)" \
    FEED_AUTHOR_NAME="$(cat /run/secrets/FEED_AUTHOR_NAME)" \
    FEED_AUTHOR_EMAIL="$(cat /run/secrets/FEED_AUTHOR_EMAIL)" \
    ./update-feed.sh

# install supercronic and crontab

RUN go install github.com/aptible/supercronic@latest
COPY crontab /
RUN supercronic -test ./crontab

# install goStatic

RUN go install github.com/PierreZ/goStatic@latest

# start supercronic and goStatic

COPY ./entrypoint.sh /
ENTRYPOINT ["./entrypoint.sh"]
