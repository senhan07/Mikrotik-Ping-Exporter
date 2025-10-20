# MikroTik High-Concurrency Ping Pusher

This is a high-performance, concurrent agent for running ping tests from a MikroTik router and pushing the results to a Prometheus remote write endpoint.

## Features

- **High Concurrency:** Uses a pool of SSH connections to run many ping tests in parallel, suitable for monitoring hundreds of targets.
- **Push-Based:** Actively pushes metrics to Prometheus, so no scraping configuration is needed on the Prometheus server.
- **Dynamic Labels:** Reads targets and labels from a YAML file.
- **Robust Parsing:** Compatible with both RouterOS v6 and v7 ping output formats.
- **SSH Failover:** Supports an alternate host for SSH connections, providing high availability.

## Installation

1.  **Clone the repository:**
    ```bash
    git clone <repository-url>
    cd <repository-directory>
    ```

2.  **Install dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

## Configuration

1.  **Create `targets.yml`:**
    Create a file named `targets.yml` that defines the targets you want to ping and the labels you want to associate with them. The format is a semicolon-delimited string:
    `<exporter-address>;<ping-target>;<module>;<zone>;<service>;<device-type>;<connection-type>;<provider>;<description>`

    **Example `targets.yml`:**
    ```yaml
    - targets:
      - 127.0.0.1:9115;43.252.9.241;icmp;INT;UPLINK;ROUTER;FO;FIBERSTAR;FIBERSTAR-9.241
      - 127.0.0.1:9115;43.252.9.249;icmp;INT;UPLINK;ROUTER;FO;LINTAS;LINTAS-9.249
    ```

## Usage

To run the agent, you must provide the MikroTik's host, user, and password, as well as the path to your `targets.yml` file and your Prometheus remote write URL.

| Argument | Description | Default |
|---|---|---|
| `--host` | The primary IP address of the MikroTik router. | **Required** |
| `--host.alt` | An alternate IP address for the MikroTik router for failover. | `None` |
| `--user` | The SSH username for the MikroTik router. | **Required** |
| `--password` or `--pass` | The SSH password for the MikroTik router. | **Required** |
| `--port.ssh` | The SSH port for the MikroTik router. | `22` |
| `--sessions` | The number of concurrent SSH sessions in the connection pool. | `5` |
| `--targets` | The path to your `targets.yml` file. | **Required** |
| `--remote-write-url` | The URL of your Prometheus remote write endpoint. | **Required** |
| `--interval` | The interval, in seconds, at which to run the probes. | `5` |

**Example:**
```bash
python3 main.py \
  --host 192.168.88.1 \
  --user myuser \
  --pass mypassword \
  --targets targets.yml \
  --remote-write-url http://localhost:9090/api/v1/write
```

## Metrics

The agent pushes the following metrics to Prometheus:

- `mikrotik_ping_rtt_seconds`: The round-trip time of the ping.
- `mikrotik_ping_up`: `1` if the target is reachable, `0` otherwise.
- `mikrotik_ping_ttl`: The time-to-live of the ping response.
- `mikrotik_ping_size_bytes`: The size of the ping packet.
