FROM --platform=$BUILDPLATFORM golang:1.26.9@sha256:37cfcca63222ec499bec5e8ad7aad52dbf7529db36d5812b8eae2f0621aa92d6 AS build
ARG TARGETOS TARGETARCH
WORKDIR /operator
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN --mount=type=cache,target=/root/.cache/go-build CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH go build -trimpath -ldflags="-s -w" -o /manager ./cmd
FROM gcr.io/distroless/static:nonroot@sha256:e2e927ec666bae08560abb3c55d0659eceabb657f56b6782ab500a9fc7f555e3
COPY --from=build /manager /manager
USER 65532:65532
ENTRYPOINT ["/manager"]
