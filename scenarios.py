import argparse
import csv
import os
import sys
import time

import run_experiment as rx

BASE_PORT = 5201
FIELDS = ['name', 'port', 'offered_mbps', 'loss_cfg_pct', 'duration_s', 'start_epoch',
          'end_epoch', 'down_epoch', 'up_epoch', 'tx_pkts', 'rx_pkts', 'jitter_ms']


def scenarios(a):
    return [
        {'name': 'congestion', 'rate': a.congestion_rate, 'loss': 0.0, 'fail': None},
        {'name': 'packet_loss', 'rate': a.loss_rate, 'loss': a.loss_pct, 'fail': None},
        {'name': 'link_failure', 'rate': a.loss_rate, 'loss': 0.0,
         'fail': (a.fail_at, a.fail_at + a.fail_for)},
    ]


def run_scenario(sc, port, a, results):
    net = rx.build(a.bw, a.delay, sc['loss'], a.queue)
    net.start()
    t_down = t_up = ''
    try:
        net.waitConnected()
        net.pingAll()
        h1, h3 = net.get('h1', 'h3')
        srv_csv = os.path.join(results, 'scn_%s_server.csv' % sc['name'])
        cli_csv = os.path.join(results, 'scn_%s_client.csv' % sc['name'])
        srv = rx.start_server(h3, port, a.duration + 6, srv_csv)
        time.sleep(0.5)
        t_start = time.time()
        cli = rx.start_client(h1, h3.IP(), port, sc['rate'], a.size, a.duration, cli_csv)
        if sc['fail']:
            time.sleep(sc['fail'][0])
            net.configLinkStatus('s1', 's2', 'down')
            t_down = time.time()
            time.sleep(sc['fail'][1] - sc['fail'][0])
            net.configLinkStatus('s1', 's2', 'up')
            t_up = time.time()
        cli_err = cli.communicate()[1]
        srv_err = srv.communicate(timeout=a.duration + 30)[1]
        t_end = time.time()
        time.sleep(a.settle)
    finally:
        net.stop()
    tx_pkts, _ = rx.parse_tx(cli_err)
    rx_pkts, _, jitter, _ = rx.parse_rx(srv_err)
    return {'name': sc['name'], 'port': port, 'offered_mbps': sc['rate'],
            'loss_cfg_pct': sc['loss'], 'duration_s': a.duration,
            'start_epoch': '%.3f' % t_start, 'end_epoch': '%.3f' % t_end,
            'down_epoch': ('%.3f' % t_down) if t_down else '',
            'up_epoch': ('%.3f' % t_up) if t_up else '',
            'tx_pkts': tx_pkts, 'rx_pkts': rx_pkts, 'jitter_ms': jitter}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--bw', type=float, default=10.0)
    ap.add_argument('--delay', default='5ms')
    ap.add_argument('--queue', type=int, default=100)
    ap.add_argument('--size', type=int, default=1000)
    ap.add_argument('--duration', type=int, default=20)
    ap.add_argument('--congestion-rate', type=float, default=15.0)
    ap.add_argument('--loss-rate', type=float, default=8.0)
    ap.add_argument('--loss-pct', type=float, default=5.0)
    ap.add_argument('--fail-at', type=float, default=6.0)
    ap.add_argument('--fail-for', type=float, default=6.0)
    ap.add_argument('--only', default='congestion,packet_loss,link_failure')
    ap.add_argument('--settle', type=float, default=3.0)
    ap.add_argument('--results', default=os.path.join(rx.ROOT, 'results'))
    a = ap.parse_args()

    for b in (rx.SERVER, rx.CLIENT):
        if not os.path.exists(b):
            sys.exit('missing %s, run: make -C udp_app' % b)
    if a.duration < a.fail_at + a.fail_for + 4:
        sys.exit('duration must be at least fail-at + fail-for + 4')

    os.makedirs(a.results, exist_ok=True)
    for name in os.listdir(a.results):
        if name.startswith('scn_') or name == 'scenarios.csv':
            os.remove(os.path.join(a.results, name))

    from mininet.log import setLogLevel
    setLogLevel('info')
    chosen = a.only.split(',')
    out = open(os.path.join(a.results, 'scenarios.csv'), 'w', newline='')
    w = csv.DictWriter(out, fieldnames=FIELDS)
    w.writeheader()
    try:
        for i, sc in enumerate(scenarios(a)):
            if sc['name'] not in chosen:
                continue
            print('scenario: %s' % sc['name'])
            row = run_scenario(sc, BASE_PORT + i, a, a.results)
            w.writerow(row)
            out.flush()
            print('  sent %d, received %d, jitter %.3f ms' % (row['tx_pkts'], row['rx_pkts'], row['jitter_ms']))
    finally:
        out.close()


if __name__ == '__main__':
    main()
