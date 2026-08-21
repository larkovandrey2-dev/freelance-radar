FROM alpine:3.21 AS build
RUN apk add --no-cache git make gcc musl-dev linux-headers
RUN git clone https://github.com/hufrea/byedpi.git /src/byedpi \
    && cd /src/byedpi \
    && git checkout ba532298de7b28cfe854aea83d061369d13ca290 \
    && make

FROM alpine:3.21
RUN adduser -D -H -u 10001 byedpi
COPY --from=build /src/byedpi/ciadpi /usr/local/bin/ciadpi
USER byedpi
ENTRYPOINT ["/bin/sh", "-c", "exec /usr/local/bin/ciadpi $BYEDPI_ARGS"]
