#!/usr/bin/env python3
import time
import re
import socket
from prometheus_client import Gauge, Info, generate_latest, CollectorRegistry
import argparse
import threading
import queue
from urllib.parse import parse_qs, urlparse
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from contextlib import contextmanager

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

    def _create_connection(self):
        return MikroTikSSHConnection(self.host, self.alt_host, self.user, self.password, self.ssh_port)

    @contextmanager
    def connection(self):
        try:
            conn = self._pool.get_nowait()
        except queue.Empty:
            conn = self._create_connection()

        try:
            if not conn.is_active():
                print("SSH connection was inactive, creating a new one.")
                conn = self._create_connection()
            yield conn
        finally:
            self._pool.put(conn)

# --- Global Metrics Registry ---
# This registry is only for metrics that are not dynamically labeled, like probe duration.
GLOBAL_REGISTRY = CollectorRegistry()
PROBE_DURATION = Gauge('mikrotik_probe_duration_seconds', 'Probe duration', registry=GLOBAL_REGISTRY)

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

# --- HTTP Handler ---
class ProbeHandler(BaseHTTPRequestHandler):
    def __init__(self, prober, *args, **kwargs):
        self.prober = prober
        super().__init__(*args, **kwargs)

    def do_GET(self):
        if self.path.startswith('/probe'):
            self.handle_probe()
        elif self.path == '/metrics':
            self.handle_metrics()
        else:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(b"<html><body><h1>MikroTik Ping Exporter</h1><p><a href='/metrics'>Metrics</a></p></body></html>")

    def handle_probe(self):
        query = parse_qs(urlparse(self.path).query)
        target = query.get('target', [''])[0]
        if not target:
            self.send_error(400, 'Missing "target" parameter')
            return

        # Extract dynamic labels from query parameters
        dynamic_labels = {k: v[0] for k, v in query.items() if k not in ['target']}

        resolved_ip = self.prober.resolve_target_ip(target)
        result = self.prober.ping_target(resolved_ip)

        # Create a temporary registry for this request
        probe_registry = CollectorRegistry()

        label_keys = ['target'] + list(dynamic_labels.keys())

        # Define metrics for the temporary registry
        ping_rtt_probe = Gauge('mikrotik_ping_rtt_seconds', 'Round-trip time', label_keys, registry=probe_registry)
        ping_up_probe = Gauge('mikrotik_ping_up', 'Target is reachable', label_keys, registry=probe_registry)
        ping_ttl_probe = Gauge('mikrotik_ping_ttl', 'Time-to-live', label_keys, registry=probe_registry)
        ping_size_probe = Gauge('mikrotik_ping_size_bytes', 'Packet size', label_keys, registry=probe_registry)
        probe_duration_probe = Gauge('mikrotik_probe_duration_seconds', 'Probe duration', registry=probe_registry)
        target_ip_probe = Info('mikrotik_target_ip_address', 'Resolved IP address of target', label_keys, registry=probe_registry)

        # Populate temporary metrics
        labels = {'target': target, **dynamic_labels}
        ping_rtt_probe.labels(**labels).set(result['rtt_sec'])
        ping_up_probe.labels(**labels).set(result['up'])
        ping_ttl_probe.labels(**labels).set(result['ttl'])
        ping_size_probe.labels(**labels).set(result['size'])
        probe_duration_probe.set(result['duration'])
        target_ip_probe.labels(**labels).info({'ip': resolved_ip})

        PROBE_DURATION.set(result['duration'])

        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; version=0.0.4')
        self.end_headers()
        try:
            self.wfile.write(generate_latest(probe_registry))
        except ConnectionAbortedError:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Client closed connection early for target {target}")
            return

        timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
        status = '🟢' if result['up'] else '🔴'
        rtt_ms = result['rtt_sec'] * 1000
        print(f"[{timestamp}] {status} {target:<15} -> {resolved_ip:<15} | rtt:{rtt_ms:.2f}ms | ttl:{result['ttl']} | size:{result['size']} | dur:{result['duration']:.2f}s")

    def handle_metrics(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; version=0.0.4')
        self.end_headers()
        self.wfile.write(generate_latest(GLOBAL_REGISTRY))

    def log_message(self, format, *args):
        # Suppress routine server logging
        pass

def run_server(prober, port=9642):
    server_address = ('0.0.0.0', port)
    httpd = ThreadingHTTPServer(server_address, lambda *args, **kwargs: ProbeHandler(prober, *args, **kwargs))
    print(f"🚀 MikroTik High-Concurrency Ping Exporter on port {port}")
    print(f"📖 Usage: http://127.0.0.1:{port}/probe?target=google.com")
    print(f"📖 Metrics: http://127.0.0.1:{port}/metrics")
    httpd.serve_forever()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='MikroTik Ping Exporter')
    parser.add_argument('--host', required=True, help='MikroTik host (primary)')
    parser.add_argument('--host.alt', dest='host_alt', help='Alternate MikroTik host (for failover)')
    parser.add_argument('--user', required=True, help='MikroTik user')
    parser.add_argument('--password', '--pass', dest='password', required=True, help='MikroTik password')
    parser.add_argument('--port.probe', dest='port_probe', type=int, default=9642, help='Port for the exporter to listen on')
    parser.add_argument('--port.ssh', dest='port_ssh', type=int, default=22, help='SSH port for the MikroTik router')
    parser.add_argument('--sessions', type=int, default=10, help='Number of concurrent SSH sessions')
    args = parser.parse_args()

    print("🚀 Starting MikroTik Ping Exporter...")
    ssh_pool = MikroTikSSHConnectionPool(args.host, args.host_alt, args.user, args.password, args.port_ssh, args.sessions)
    prober = MikroTikPingProber(ssh_pool)
    run_server(prober, args.port_probe)
