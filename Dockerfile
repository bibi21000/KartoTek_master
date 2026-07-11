# syntax=docker/dockerfile:1

# base python image for custom image
FROM python:3.14-alpine

ENV PIP_NO_CACHE_DIR=false
ENV KARTOTEK_MASTER_CONF=/app/conf/config.conf

# create working directory and install pip dependencies
RUN mkdir -p /data
RUN mkdir -p /logs
RUN mkdir -p /html
COPY pyproject.toml /app/
COPY src /app/src/
COPY gunicorn.conf.py /app/
RUN mkdir -p /conf
COPY docker/config.conf /app/conf/
COPY docker/servers.json /app/conf/
WORKDIR /app
RUN pip3 install .[prod]

EXPOSE 8003

VOLUME ["/data"]

# run the flask server
CMD [ "gunicorn", "-c", "gunicorn.conf.py", "kartotek_master.wsgi:app"]
