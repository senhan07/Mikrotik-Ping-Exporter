#!/usr/bin/env python3
import paramiko
import time
import re
import socket
from prometheus_client import Gauge, Histogram, Info, generate_latest
import yaml
import threading
from urllib.parse import parse_qs, urlparse
from http.server import BaseHTTPRequestHandler, HTTPServer

# Load config
with open('config.yml', 'r') as f:
    config = yaml.safe_load(f)

SSH_HOST = config['mikrotik']['host']
SSH_USER = config['mikrotik']['user'] 
SSH_PASS = config['mikrotik']['password']

# Metrics
PING_RTT = Histogram('mikrotik_ping_rtt_seconds', 'RTT distribution buckets (burst pings)', ['target', 'job'],
                     buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, float('inf')])
PING_LOSS = Gauge('mikrotik_ping_loss_percent', 'Packet loss %', ['target', 'job'])
PING_UP = Gauge('mikrotik_ping_up', 'Target is reachable', ['target', 'job'])
PING_SENT = Gauge('mikrotik_ping_sent_packets', 'Packets sent', ['target', 'job'])
PING_RECV = Gauge('mikrotik_ping_received_packets', 'Packets received', ['target', 'job'])
PING_TTL = Gauge('mikrotik_ping_ttl', 'TTL from first packet', ['target', 'job'])
PING_SIZE = Gauge('mikrotik_ping_size_bytes', 'Packet size', ['target', 'job'])
PROBE_DURATION = Gauge('mikrotik_probe_duration_seconds', 'Probe duration')

# ✅ FIXED: Info metric for STRING IPs!
TARGET_IP = Info('mikrotik_target_ip_address', 'Resolved IP address of target', ['target', 'job'])

class MikroTikPingProber:
    def __init__(self):
        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.lock = threading.Lock()
        self.connect()
    
    def connect(self):
        with self.lock:
            self.ssh.connect(SSH_HOST, username=SSH_USER, password=SSH_PASS)
    
    def reconnect_if_needed(self):
        try:
            transport = self.ssh.get_transport()
            if not transport or not transport.is_active():
                self.connect()
        except:
            self.connect()
    
    def resolve_target_ip(self, target):
        """Resolve domain to IP - updates over time!"""
        try:
            ip = socket.gethostbyname(target)
            return ip
        except:
            return target  # Return original if IP
    
    def ping_burst_target(self, target_ip, count=5, burst=10):
        self.reconnect_if_needed()
        
        start_time = time.time()
        interval_ms = max(10, 1000 // burst)
        with self.lock:
            cmd = f'/ping {target_ip} count={count} interval={interval_ms}ms'
            stdin, stdout, stderr = self.ssh.exec_command(cmd)
            output = stdout.read().decode().strip()
        
        duration = time.time() - start_time
        
        # Parse burst: Extract each RTT
        rtts_sec = []
        ttl = 0
        size = 56
        
        seq_matches = re.findall(r'^\s*\d+\s+[\d.]+\s+(\d+)\s+(\d+)\s+(\d+)ms', output, re.MULTILINE)
        for match in seq_matches:
            size = int(match[0])
            ttl = int(match[1]) if not ttl else ttl
            rtt_ms = int(match[2])
            rtts_sec.append(rtt_ms / 1000.0)
        
        # Summary parsing
        sent_match = re.search(r'sent=(\d+)', output)
        recv_match = re.search(r'received=(\d+)', output)
        loss_match = re.search(r'packet-loss=(\d+)%', output)
        
        sent = int(sent_match.group(1)) if sent_match else len(seq_matches)
        recv = int(recv_match.group(1)) if recv_match else len(seq_matches)
        loss = float(loss_match.group(1)) if loss_match else ((sent - recv) / sent * 100 if sent > 0 else 100.0)
        up = recv > 0
        
        return {
            'rtts_sec': rtts_sec,
            'loss': loss,
            'up': 1 if up else 0,
            'sent': sent,
            'recv': recv,
            'ttl': ttl,
            'size': size,
            'duration': duration,
            'burst_pps': burst
        }

# HTTP Handler
class ProbeHandler(BaseHTTPRequestHandler):
    def __init__(self, prober, *args, **kwargs):
        self.prober = prober
        super().__init__(*args, **kwargs)
    
    def do_GET(self):
        if self.path.startswith('/probe'):
            self.handle_probe()
        else:
            self.handle_metrics()
    
    def handle_probe(self):
        query = parse_qs(urlparse(self.path).query)
        target = query.get('target', [''])[0]
        count = int(query.get('count', ['10'])[0])
        burst = int(query.get('burst', ['10'])[0])
        job = query.get('job', ['mikrotik'])[0]
        
        if not target:
            self.send_error(400, 'Missing "target" parameter')
            return
        
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; version=0.0.4')
        self.end_headers()
        
        # ✅ RESOLVE IP (changes over time!)
        resolved_ip = self.prober.resolve_target_ip(target)
        
        result = self.prober.ping_burst_target(resolved_ip, count, burst)
        
        # Histogram (accumulates forever)
        for rtt in result['rtts_sec']:
            PING_RTT.labels(target=target, job=job).observe(rtt)
        
        # Gauges (current values)
        PING_LOSS.labels(target=target, job=job).set(result['loss'])
        PING_UP.labels(target=target, job=job).set(result['up'])
        PING_SENT.labels(target=target, job=job).set(result['sent'])
        PING_RECV.labels(target=target, job=job).set(result['recv'])
        PING_TTL.labels(target=target, job=job).set(result['ttl'])
        PING_SIZE.labels(target=target, job=job).set(result['size'])
        PROBE_DURATION.set(result['duration'])
        
        # ✅ FIXED: Info metric for STRING IPs!
        TARGET_IP.labels(target=target, job=job).info({'ip': resolved_ip})
        
        # Single-line log
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
        status = '🟢' if result['up'] else '🔴'
        avg_rtt = sum(result['rtts_sec']) / len(result['rtts_sec']) if result['rtts_sec'] else 999
        interval = 1000 // burst
        print(f"[{timestamp}] {status} {target:<15} → {resolved_ip:<15} | Burst:{burst}pps({interval}ms) | Pkts:{count} | Avg:{avg_rtt:.3f}s | TTL:{result['ttl']:3} | Loss:{result['loss']:5.1f}% | {result['sent']}/{result['recv']} | ⚡{result['duration']:.2f}s | 📈+{len(result['rtts_sec'])}")
        
        self.wfile.write(generate_latest())
    
    def handle_metrics(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; version=0.0.4')
        self.end_headers()
        self.wfile.write(generate_latest())
    
    def log_message(self, format, *args):
        pass

def run_servers(prober, port=9642):
    server = HTTPServer(('localhost', port), lambda *args: ProbeHandler(prober, *args))
    print(f"🚀 MikroTik IP RESOLUTION Exporter on :{port}")
    print("📖 Usage: /probe?target=google.com&count=10&burst=10")
    server.serve_forever()

if __name__ == '__main__':
    print("🚀 Starting MikroTik IP RESOLUTION Ping Exporter...")
    prober = MikroTikPingProber()
    run_servers(prober, 9642)