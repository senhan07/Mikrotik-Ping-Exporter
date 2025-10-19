# MikroTik High-Concurrency Ping Exporter

This is a high-performance, concurrent Prometheus exporter for running ping tests from a MikroTik router. It is designed to be used in a "blackbox" style, where Prometheus can request probes to any target and receive metrics in response.

## Features

- **High Concurrency:** Uses a pool of SSH connections to run many ping tests in parallel, suitable for monitoring hundreds of targets.
- **Dynamic Labels:** Supports passing arbitrary labels to the exporter via URL parameters, similar to the official Blackbox Exporter.
- **Secure Configuration:** Includes a utility to encrypt the MikroTik password, so it is not stored in plaintext.
- **Robust Parsing:** Compatible with both RouterOS v6 and v7 ping output formats.
- **Blackbox Style:** Designed to be scraped by Prometheus with a configuration that passes the target as a parameter.

## Installation

1.  **Clone the repository:**
    ```bash
    git clone <repository-url>
    cd <repository-directory>
    ```

2.  **Install dependencies:**
    ```bash
    pip install paramiko prometheus-client pyyaml cryptography
    ```

## Configuration

1.  **Generate an encryption key:**
    The first time you run the encryption script, it will generate a `secret.key` file. This file is essential for encrypting and decrypting your password. **Do not lose this file.**
    ```bash
    python3 encrypt_pass.py
    ```

2.  **Encrypt your password:**
    Run the `encrypt_pass.py` script again and enter your MikroTik's SSH password when prompted. The script will output an encrypted password string.
    ```bash
    python3 encrypt_pass.py
    ```

3.  **Create `config.yml`:**
    Create a file named `config.yml` and add your MikroTik's connection details. Use the encrypted password you generated in the previous step.
    ```yaml
    mikrotik:
      host: 192.168.88.1  # Your MikroTik's IP address
      user: your-ssh-user
      password: 'your-encrypted-password'
    ```

## Usage

To run the exporter:
```bash
python3 main.py
```
The exporter will start on port `9642`.

## Prometheus Configuration

You can use this exporter with a Prometheus configuration similar to the standard Blackbox Exporter. Here is an example `prometheus.yml` snippet:

```yaml
scrape_configs:
  - job_name: 'mikrotik-blackbox'
    metrics_path: /probe
    params:
      module: [icmp] # This is just an example; the exporter doesn't use modules
    static_configs:
      - targets:
        - google.com
        - cloudflare.com
        labels:
          zone: 'us-east-1'
          service: 'dns'
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - source_labels: [__param_target]
        target_label: instance
      - target_label: __address__
        replacement: 127.0.0.1:9642  # The address of the exporter
```

This configuration will scrape the exporter and pass the `target` and any other defined labels as URL parameters.

## Metrics

The exporter exposes the following metrics:

- `mikrotik_ping_rtt_seconds`: The round-trip time of the ping.
- `mikrotik_ping_up`: `1` if the target is reachable, `0` otherwise.
- `mikrotik_ping_ttl`: The time-to-live of the ping response.
- `mikrotik_ping_size_bytes`: The size of the ping packet.
- `mikrotik_probe_duration_seconds`: The duration of the entire probe, including the SSH connection.
- `mikrotik_target_ip_address_info`: An `Info` metric that contains the resolved IP address of the target.
