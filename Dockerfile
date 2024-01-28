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

RUN curl -o /wantlist.pickle http://deals.internal:8043/wantlist.pickle
RUN curl -o /searches.pickle http://deals.internal:8043/searches.pickle
RUN mkdir -p /srv/http
RUN curl -o /srv/http/index.xml http://deals.internal:8043/index.xml
RUN cp /wantlist.pickle /srv/http/wantlist.pickle
RUN cp /searches.pickle /srv/http/searches.pickle
COPY update-feed.sh /
COPY update-wantlist.sh /
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
    /venv/bin/python -I \
    -m deals.main \
    --quiet \
    --feed /srv/http/index.xml \
    --minutes 1

# install supercronic and crontab

RUN go install github.com/aptible/supercronic@latest
COPY crontab /
RUN supercronic -test ./crontab

# install goStatic

RUN go install github.com/PierreZ/goStatic@latest

# start supercronic and goStatic

COPY ./entrypoint.sh /
ENTRYPOINT ["./entrypoint.sh"]
