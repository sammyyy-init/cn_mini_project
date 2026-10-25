import argparse
import csv
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(ROOT, 'udp_app', 'server')
CLIENT = os.path.join(ROOT, 'udp_app', 'client')
BASE_PORT = 5001
BG_PORT = 6001
FIELDS = ['run', 'port', 'offered_mbps', 'size', 'duration_s', 'tx_pkts', 'tx_bytes',
          'rx_pkts', 'rx_bytes', 'jitter_ms', 'mean_delay_ms']

TX_RE = re.compile(r'flow (\d+): sent (\d+) pkts, (\d+) bytes')
RX_RE = re.compile(r'flow (\d+): rx=(\d+) pkts, (\d+) bytes, expected=(\d+), '
                   r'loss=([\d.]+)%, jitter=([\d.]+) ms, delay=([\d.]+) ms')


def parse_tx(text):
    m = TX_RE.search(text)
    return (int(m.group(2)), int(m.group(3))) if m else (0, 0)


def parse_rx(text):
    m = RX_RE.search(text)
    if not m:
        return 0, 0, 0.0, 0.0
    return int(m.group(2)), int(m.group(3)), float(m.group(6)), float(m.group(7))


def build(bw, delay, loss, queue):
    from mininet.net import Mininet
    from mininet.node import RemoteController, OVSSwitch
    from mininet.link import TCLink
    net = Mininet(controller=None, switch=OVSSwitch, link=TCLink, autoSetMacs=True)
    net.addController('c0', controller=RemoteController, ip='127.0.0.1', port=6653)
    h1 = net.addHost('h1', ip='10.0.0.1/24')
    h2 = net.addHost('h2', ip='10.0.0.2/24')
    h3 = net.addHost('h3', ip='10.0.0.3/24')
    h4 = net.addHost('h4', ip='10.0.0.4/24')
    s1 = net.addSwitch('s1', protocols='OpenFlow13')
    s2 = net.addSwitch('s2', protocols='OpenFlow13')
    for h in (h1, h2):
        net.addLink(h, s1, bw=100)
    for h in (h3, h4):
        net.addLink(h, s2, bw=100)
    net.addLink(s1, s2, bw=bw, delay=delay, loss=loss, max_queue_size=queue)
    return net


def start_server(host, port, secs, path):
    cmd = [SERVER, '-p', str(port), '-n', '1', '-t', str(secs), '-o', path]
    return host.popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                      universal_newlines=True)


def start_client(host, dst, port, rate, size, secs, path):
    cmd = [CLIENT, '-s', dst, '-p', str(port), '-l', str(size), '-r', str(rate),
           '-t', str(secs), '-n', '1', '-o', path]
    return host.popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                      universal_newlines=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--bw', type=float, default=10.0)
    ap.add_argument('--delay', default='5ms')
    ap.add_argument('--loss', type=float, default=0.0)
    ap.add_argument('--queue', type=int, default=100)
    ap.add_argument('--rates', default='1,2,4,6,8,9,10,12,15')
    ap.add_argument('--size', type=int, default=1000)
    ap.add_argument('--duration', type=int, default=10)
    ap.add_argument('--bg', type=float, default=0.0)
    ap.add_argument('--settle', type=float, default=2.5)
    ap.add_argument('--results', default=os.path.join(ROOT, 'results'))
    a = ap.parse_args()

    for b in (SERVER, CLIENT):
        if not os.path.exists(b):
            sys.exit('missing %s, run: make -C udp_app' % b)

    rates = [float(x) for x in a.rates.split(',')]
    os.makedirs(a.results, exist_ok=True)
    for name in os.listdir(a.results):
        if name.startswith(('server_', 'client_', 'bg_')) or name == 'udp_summary.csv':
            os.remove(os.path.join(a.results, name))

    from mininet.log import setLogLevel
    setLogLevel('info')
    net = build(a.bw, a.delay, a.loss, a.queue)
    net.start()
    summary = open(os.path.join(a.results, 'udp_summary.csv'), 'w', newline='')
    w = csv.DictWriter(summary, fieldnames=FIELDS)
    w.writeheader()
    try:
        net.waitConnected()
        net.pingAll()
        h1, h2, h3, h4 = net.get('h1', 'h2', 'h3', 'h4')

        for i, rate in enumerate(rates):
            port = BASE_PORT + i
            print('run %d/%d: offered %.2f Mbps, udp port %d' % (i + 1, len(rates), rate, port))
            srv_csv = os.path.join(a.results, 'server_%d.csv' % i)
            cli_csv = os.path.join(a.results, 'client_%d.csv' % i)
            srv = start_server(h3, port, a.duration + 4, srv_csv)
            bg_srv = bg_cli = None
            if a.bg > 0:
                bg_srv = start_server(h4, BG_PORT + i, a.duration + 4,
                                      os.path.join(a.results, 'bg_server_%d.csv' % i))
            time.sleep(0.5)
            cli = start_client(h1, h3.IP(), port, rate, a.size, a.duration, cli_csv)
            if a.bg > 0:
                bg_cli = start_client(h2, h4.IP(), BG_PORT + i, a.bg, a.size, a.duration,
                                      os.path.join(a.results, 'bg_client_%d.csv' % i))
            cli_err = cli.communicate()[1]
            if bg_cli is not None:
                bg_cli.communicate()
            srv_err = srv.communicate(timeout=a.duration + 30)[1]
            if bg_srv is not None:
                bg_srv.communicate(timeout=a.duration + 30)

            tx_pkts, tx_bytes = parse_tx(cli_err)
            rx_pkts, rx_bytes, jitter, delay = parse_rx(srv_err)
            row = {'run': i, 'port': port, 'offered_mbps': rate, 'size': a.size,
                   'duration_s': a.duration, 'tx_pkts': tx_pkts, 'tx_bytes': tx_bytes,
                   'rx_pkts': rx_pkts, 'rx_bytes': rx_bytes, 'jitter_ms': jitter,
                   'mean_delay_ms': delay}
            w.writerow(row)
            summary.flush()
            print('  sent %d, received %d, jitter %.3f ms' % (tx_pkts, rx_pkts, jitter))
            time.sleep(a.settle)
    finally:
        summary.close()
        net.stop()


if __name__ == '__main__':
    main()
