#!/usr/bin/env python3
import time
import re
import socket
import argparse
import threading
import concurrent.futures
import queue
from contextlib import contextmanager
import yaml
import requests
import snappy
from prometheus_client.remote_write.pb2 import WriteRequest, TimeSeries, Label, Sample

# --- SSH Connection Pooling ---
import paramiko

class MikroTikSSHConnection:
    def __init__(self, host, alt_host, user, password, port):
        self.host = host
        self.alt_host = alt_host
        self.user = user
        self.password = password
        self.port = port
        self.ssh = None

    def connect(self):
        try:
            self.ssh = paramiko.SSHClient()
            self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self.ssh.connect(self.host, username=self.user, password=self.password, port=self.port, timeout=10, look_for_keys=False)
            print(f"SSH connection to {self.host} successful.")
        except Exception as e:
            print(f"Error connecting to primary host {self.host}: {e}")
            if self.alt_host:
                print(f"Trying alternate host {self.alt_host}...")
                try:
                    self.ssh.connect(self.alt_host, username=self.user, password=self.password, port=self.port, timeout=10, look_for_keys=False)
                    print(f"SSH connection to {self.alt_host} successful.")
                except Exception as e2:
                    print(f"Error connecting to alternate host {self.alt_host}: {e2}")
                    self.ssh = None
            else:
                self.ssh = None

    def is_active(self):
        return self.ssh and self.ssh.get_transport() and self.ssh.get_transport().is_active()

    def exec_command(self, cmd):
        if not self.is_active():
            self.connect()
        if self.ssh:
            return self.ssh.exec_command(cmd)
        raise ConnectionError("SSH connection is not active.")

class MikroTikSSHConnectionPool:
    def __init__(self, host, alt_host, user, password, ssh_port, max_connections):
        self.host = host
        self.alt_host = alt_host
        self.user = user
        self.password = password
        self.ssh_port = ssh_port
        self.max_connections = max_connections
        self._pool = queue.Queue(maxsize=max_connections)
        for _ in range(max_connections):
            self._pool.put(self._create_connection())

    def _create_connection(self):
        return MikroTikSSHConnection(self.host, self.alt_host, self.user, self.password, self.ssh_port)

    @contextmanager
    def connection(self):
        conn = self._pool.get() # This will block until a connection is available

        try:
            if not conn.is_active():
                print("SSH connection was inactive, creating a new one.")
                conn = self._create_connection()
            yield conn
        finally:
            self._pool.put(conn)

# --- Prober ---
class MikroTikPingProber:
    def __init__(self, pool):
        self.pool = pool

    def resolve_target_ip(self, target):
        try:
            return socket.gethostbyname(target)
        except socket.gaierror:
            print(f"Could not resolve {target}, using target as-is.")
            return target

    def ping_target(self, target_ip):
        start_time = time.time()
        cmd = f'/ping {target_ip} count=1'

        output = ""
        error = ""
        try:
            with self.pool.connection() as conn:
                stdin, stdout, stderr = conn.exec_command(cmd)
                output = stdout.read().decode().strip()
                error = stderr.read().decode().strip()
                if error:
                    print(f"Error from MikroTik for target {target_ip}: {error}")
        except ConnectionError as e:
            print(f"SSH connection error for target {target_ip}: {e}")
            return self._error_result(duration=time.time() - start_time)

        duration = time.time() - start_time
        return self._parse_ping_output(output, duration)

    def _parse_ping_output(self, output, duration):
        # RouterOS v6 Style
        match_v6 = re.search(r'^\s*\d+\s+[\d.]+\s+(\d+)\s+(\d+)\s+(\d+)ms', output, re.MULTILINE)

        # RouterOS v7 Style
        rtt_match_v7 = re.search(r'time=(\d+\.?\d*)ms', output)
        ttl_match_v7 = re.search(r'ttl=(\d+)', output)

        if match_v6 and "ttl-exceeded" not in output:
            size = int(match_v6.group(1))
            ttl = int(match_v6.group(2))
            rtt_sec = float(match_v6.group(3)) / 1000.0
            up = 1
        elif rtt_match_v7:
            rtt_sec = float(rtt_match_v7.group(1)) / 1000.0
            ttl = int(ttl_match_v7.group(1)) if ttl_match_v7 else 0
            size = 56  # Default size for v7 style
            up = 1
        else:
            up = 0
            rtt_sec = 0
            ttl = 0
            size = 0

        if "timeout" in output or "no route to host" in output:
            up = 0

        return {
            'rtt_sec': rtt_sec, 'up': up, 'ttl': ttl, 'size': size,
            'duration': duration
        }

    def _error_result(self, duration):
        return { 'rtt_sec': 0, 'up': 0, 'ttl': 0, 'size': 0, 'duration': duration}

# --- Main Application Logic ---
def run_probes(prober, targets):
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=prober.pool.max_connections) as executor:
        future_to_target = {executor.submit(prober.ping_target, prober.resolve_target_ip(t['target'])): t for t in targets}
        for future in concurrent.futures.as_completed(future_to_target):
            target_info = future_to_target[future]
            try:
                result = future.result()
                results.append({'labels': target_info['labels'], 'result': result})
            except Exception as exc:
                print(f'{target_info["labels"]["target"]} generated an exception: {exc}')
    return results

def push_to_prometheus(url, results):
    write_request = WriteRequest()

    for res in results:
        labels = [Label(name=k, value=v) for k, v in res['labels'].items()]

        # RTT
        ts_rtt = TimeSeries()
        ts_rtt.labels.extend([Label(name="__name__", value="mikrotik_ping_rtt_seconds")] + labels)
        ts_rtt.samples.append(Sample(value=res['result']['rtt_sec'], timestamp=int(time.time() * 1000)))
        write_request.timeseries.append(ts_rtt)

        # Up
        ts_up = TimeSeries()
        ts_up.labels.extend([Label(name="__name__", value="mikrotik_ping_up")] + labels)
        ts_up.samples.append(Sample(value=res['result']['up'], timestamp=int(time.time() * 1000)))
        write_request.timeseries.append(ts_up)

        # TTL
        ts_ttl = TimeSeries()
        ts_ttl.labels.extend([Label(name="__name__", value="mikrotik_ping_ttl")] + labels)
        ts_ttl.samples.append(Sample(value=res['result']['ttl'], timestamp=int(time.time() * 1000)))
        write_request.timeseries.append(ts_ttl)

        # Size
        ts_size = TimeSeries()
        ts_size.labels.extend([Label(name="__name__", value="mikrotik_ping_size_bytes")] + labels)
        ts_size.samples.append(Sample(value=res['result']['size'], timestamp=int(time.time() * 1000)))
        write_request.timeseries.append(ts_size)

    uncompressed = write_request.SerializeToString()
    compressed = snappy.compress(uncompressed)

    headers = {
        "Content-Encoding": "snappy",
        "Content-Type": "application/x-protobuf",
        "X-Prometheus-Remote-Write-Version": "0.1.0"
    }

    try:
        response = requests.post(url, headers=headers, data=compressed)
        if response.status_code != 204:
            print(f"Error pushing to Prometheus: {response.status_code} {response.text}")
    except Exception as e:
        print(f"Error pushing to Prometheus: {e}")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='MikroTik Ping Exporter')
    parser.add_argument('--host', required=True, help='MikroTik host (primary)')
    parser.add_argument('--host.alt', dest='host_alt', help='Alternate MikroTik host (for failover)')
    parser.add_argument('--user', required=True, help='MikroTik user')
    parser.add_argument('--password', '--pass', dest='password', required=True, help='MikroTik password')
    parser.add_argument('--port.ssh', dest='port_ssh', type=int, default=22, help='SSH port for the MikroTik router')
    parser.add_argument('--sessions', type=int, default=5, help='Number of concurrent SSH sessions')
    parser.add_argument('--targets', required=True, help='Path to targets.yml file')
    parser.add_argument('--remote-write-url', required=True, help='Prometheus remote write URL')
    parser.add_argument('--interval', type=int, default=5, help='Scrape interval in seconds')
    args = parser.parse_args()

    with open(args.targets) as f:
        targets_config = yaml.safe_load(f)

    parsed_targets = []
    for target_group in targets_config:
        for target_string in target_group['targets']:
            parts = target_string.split(';')
            parsed_targets.append({
                'target': parts[1],
                'labels': {
                    'target': parts[1],
                    'module': parts[2],
                    'zone': parts[3],
                    'service': parts[4],
                    'device_type': parts[5],
                    'connection_type': parts[6],
                    'provider': parts[7],
                    'description': parts[8]
                }
            })

    ssh_pool = MikroTikSSHConnectionPool(args.host, args.host_alt, args.user, args.password, args.port_ssh, args.sessions)
    prober = MikroTikPingProber(ssh_pool)

    while True:
        probe_results = run_probes(prober, parsed_targets)
        push_to_prometheus(args.remote_write_url, probe_results)
        time.sleep(args.interval)
