#!/usr/bin/env python3
import paramiko
import time
import re
import socket
from prometheus_client import Gauge, Info, generate_latest, CollectorRegistry
import yaml
import threading
import queue
from urllib.parse import parse_qs, urlparse
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from contextlib import contextmanager
from functools import lru_cache

# Load config
with open('config.yml', 'r') as f:
    config = yaml.safe_load(f)

SSH_HOST = config['mikrotik']['host']
SSH_USER = config['mikrotik']['user']
SSH_PASS = config['mikrotik']['password']
MAX_SSH_CONNECTIONS = int(config.get('max_ssh_connections', 10))

# --- SSH Connection Pooling ---
class MikroTikSSHConnection:
    def __init__(self, host, user, password):
        self.host = host
        self.user = user
        self.password = password
        self.ssh = None
        self.connect()

    def connect(self):
        try:
            self.ssh = paramiko.SSHClient()
            self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self.ssh.connect(self.host, username=self.user, password=self.password, timeout=10)
            print("SSH connection successful.")
        except Exception as e:
            print(f"Error connecting to SSH: {e}")
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
    def __init__(self, max_connections):
        self.max_connections = max_connections
        self._pool = queue.Queue(maxsize=max_connections)
        for _ in range(max_connections):
            self._pool.put(self._create_connection())

    def _create_connection(self):
        return MikroTikSSHConnection(SSH_HOST, SSH_USER, SSH_PASS)

    @contextmanager
    def connection(self):
        conn = self._pool.get()
        try:
            if not conn.is_active():
                print("SSH connection was inactive, creating a new one.")
                conn = self._create_connection()
            yield conn
        finally:
            self._pool.put(conn)

# --- Global Metrics Registry ---
GLOBAL_REGISTRY = CollectorRegistry()
PING_RTT = Gauge('mikrotik_ping_rtt_seconds', 'Round-trip time', ['target', 'job'], registry=GLOBAL_REGISTRY)
PING_UP = Gauge('mikrotik_ping_up', 'Target is reachable', ['target', 'job'], registry=GLOBAL_REGISTRY)
PING_TTL = Gauge('mikrotik_ping_ttl', 'Time-to-live', ['target', 'job'], registry=GLOBAL_REGISTRY)
PING_SIZE = Gauge('mikrotik_ping_size_bytes', 'Packet size', ['target', 'job'], registry=GLOBAL_REGISTRY)
PROBE_DURATION = Gauge('mikrotik_probe_duration_seconds', 'Probe duration', registry=GLOBAL_REGISTRY)
TARGET_IP = Info('mikrotik_target_ip_address', 'Resolved IP address of target', ['target', 'job'], registry=GLOBAL_REGISTRY)

# --- Prober ---
class MikroTikPingProber:
    def __init__(self, pool):
        self.pool = pool

    @lru_cache(maxsize=1024)
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
        match = re.search(r'^\s*\d+\s+[\d.]+\s+(\d+)\s+(\d+)\s+(\d+)ms', output, re.MULTILINE)

        if match:
            size = int(match.group(1))
            ttl = int(match.group(2))
            rtt_sec = float(match.group(3)) / 1000.0
            up = 1
        else:
            up = 0
            rtt_sec = 0
            ttl = 0
            size = 0

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

        job = query.get('job', ['mikrotik-exporter'])[0]

        resolved_ip = self.prober.resolve_target_ip(target)
        result = self.prober.ping_target(resolved_ip)

        # Create a temporary registry for this request
        probe_registry = CollectorRegistry()

        # Define metrics for the temporary registry
        ping_rtt_probe = Gauge('mikrotik_ping_rtt_seconds', 'Round-trip time', ['target', 'job'], registry=probe_registry)
        ping_up_probe = Gauge('mikrotik_ping_up', 'Target is reachable', ['target', 'job'], registry=probe_registry)
        ping_ttl_probe = Gauge('mikrotik_ping_ttl', 'Time-to-live', ['target', 'job'], registry=probe_registry)
        ping_size_probe = Gauge('mikrotik_ping_size_bytes', 'Packet size', ['target', 'job'], registry=probe_registry)
        probe_duration_probe = Gauge('mikrotik_probe_duration_seconds', 'Probe duration', registry=probe_registry)
        target_ip_probe = Info('mikrotik_target_ip_address', 'Resolved IP address of target', ['target', 'job'], registry=probe_registry)

        # Populate temporary metrics
        ping_rtt_probe.labels(target=target, job=job).set(result['rtt_sec'])
        ping_up_probe.labels(target=target, job=job).set(result['up'])
        ping_ttl_probe.labels(target=target, job=job).set(result['ttl'])
        ping_size_probe.labels(target=target, job=job).set(result['size'])
        probe_duration_probe.set(result['duration'])
        target_ip_probe.labels(target=target, job=job).info({'ip': resolved_ip})

        # Update global metrics for /metrics endpoint
        PING_RTT.labels(target=target, job=job).set(result['rtt_sec'])
        PING_UP.labels(target=target, job=job).set(result['up'])
        PING_TTL.labels(target=target, job=job).set(result['ttl'])
        PING_SIZE.labels(target=target, job=job).set(result['size'])
        PROBE_DURATION.set(result['duration'])
        TARGET_IP.labels(target=target, job=job).info({'ip': resolved_ip})

        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; version=0.0.4')
        self.end_headers()
        self.wfile.write(generate_latest(probe_registry))

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
    try:
        print("🚀 Starting MikroTik Ping Exporter...")
        ssh_pool = MikroTikSSHConnectionPool(max_connections=MAX_SSH_CONNECTIONS)
        prober = MikroTikPingProber(ssh_pool)
        run_server(prober, 9642)
    except Exception as e:
        import traceback
        with open("error.log", "w") as f:
            f.write(traceback.format_exc())
