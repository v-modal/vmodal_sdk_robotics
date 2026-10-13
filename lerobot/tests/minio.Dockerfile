FROM golang:1.24-bookworm AS build
WORKDIR /src
# Build upstream source because official public binary/image URLs were withdrawn.
RUN git clone --depth 1 --branch RELEASE.2025-04-22T22-12-26Z https://github.com/minio/minio.git .
RUN test "$(git rev-parse HEAD)" = 0d7408fc9969caf07de6a8c3a84f9fbb10a6739e
RUN CGO_ENABLED=0 go build -trimpath -o /minio .

FROM debian:bookworm-slim
COPY --from=build /minio /usr/local/bin/minio
ENTRYPOINT ["minio"]
CMD ["server", "/data"]
