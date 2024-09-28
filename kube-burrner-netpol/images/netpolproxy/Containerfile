FROM quay.io/rsevilla/debug:latest

RUN microdnf install golang -y \
    && microdnf clean all

WORKDIR /app
COPY go.mod  *.go ./
RUN go mod download
RUN go mod tidy
RUN CGO_ENABLED=0 GOOS=linux go build -o /netpolproxy
EXPOSE 9002
CMD ["/netpolproxy"]
