# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# dev: SWI-Prolog runtime + Go toolchain. Used for `go test`, because the tests
# exercise the real swipl binary rather than mocking it.
# ---------------------------------------------------------------------------
FROM swipl:10.0.2 AS dev
COPY --from=golang:1.25-bookworm /usr/local/go /usr/local/go
ENV PATH=/usr/local/go/bin:/go/bin:$PATH \
    GOPATH=/go \
    GOFLAGS=-buildvcs=false
WORKDIR /src

# ---------------------------------------------------------------------------
# build: compile a static binary
# ---------------------------------------------------------------------------
FROM golang:1.25-bookworm AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/prolog-mcp .

# ---------------------------------------------------------------------------
# runtime: SWI-Prolog + the server binary. driver.pl is embedded in the binary
# via go:embed, so nothing else needs to be copied.
# ---------------------------------------------------------------------------
FROM swipl:10.0.2 AS runtime
# The state directory must exist in the image and be owned by the runtime user.
# Docker creates a mount point that is missing from the image as root-owned, so
# a named volume mounted at a path that does not exist here would be unwritable.
RUN useradd --create-home --uid 10001 prolog \
    && mkdir -p /home/prolog/state \
    && chown -R prolog:prolog /home/prolog
COPY --from=build /out/prolog-mcp /usr/local/bin/prolog-mcp
USER prolog
WORKDIR /home/prolog
VOLUME ["/home/prolog/state"]
ENV PROLOG_MCP_STATE=/home/prolog/state/kb.json
ENTRYPOINT ["/usr/local/bin/prolog-mcp"]
