import argparse
import os

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--results', default='results')
    ap.add_argument('--bw', type=float, default=10.0)
    ap.add_argument('--ts-rate', type=float, default=None)
    a = ap.parse_args()

    udp = pd.read_csv(os.path.join(a.results, 'udp_summary.csv'))
    flows = pd.read_csv(os.path.join(a.results, 'sdn_flows.csv'))

    rows = []
    for _, r in udp.iterrows():
        f = flows[flows.udp_dst == r['port']]
        s1 = f[f.dpid == 1]
        s2 = f[f.dpid == 2]
        b1 = s1.byte_count.max() if len(s1) else 0
        p1 = s1.packet_count.max() if len(s1) else 0
        b2 = s2.byte_count.max() if len(s2) else 0
        p2 = s2.packet_count.max() if len(s2) else 0
        dur = r['duration_s']
        rows.append({
            'run': r['run'],
            'port': r['port'],
            'offered_mbps': r['offered_mbps'],
            'sent_mbps': r['tx_bytes'] * 8 / dur / 1e6,
            'app_mbps': r['rx_bytes'] * 8 / dur / 1e6,
            'sdn_s1_mbps': b1 * 8 / dur / 1e6,
            'sdn_s2_mbps': b2 * 8 / dur / 1e6,
            'app_loss_pct': (r['tx_pkts'] - r['rx_pkts']) / r['tx_pkts'] * 100 if r['tx_pkts'] else 0.0,
            'sdn_loss_pct': (p1 - p2) / p1 * 100 if p1 > 0 else 0.0,
            'avg_frame_bytes': b2 / p2 if p2 > 0 else 0.0,
            'overhead_bytes': (b2 / p2 - r['size']) if p2 > 0 else 0.0,
            'jitter_ms': r['jitter_ms'],
            'mean_delay_ms': r['mean_delay_ms'],
        })
    summ = pd.DataFrame(rows).sort_values('offered_mbps')
    summ.round(3).to_csv(os.path.join(a.results, 'summary.csv'), index=False)
    print(summ.drop(columns=['run', 'port']).round(3).to_string(index=False))

    plt.figure(figsize=(7, 5))
    plt.plot(summ.offered_mbps, summ.offered_mbps, 'k:', label='ideal (offered)')
    plt.plot(summ.offered_mbps, summ.app_mbps, 'o-', label='app throughput (UDP payload)')
    plt.plot(summ.offered_mbps, summ.sdn_s1_mbps, 's--', label='SDN s1 (ingress, frame bytes)')
    plt.plot(summ.offered_mbps, summ.sdn_s2_mbps, '^-', label='SDN s2 (after bottleneck, frame bytes)')
    plt.axhline(a.bw, color='r', linestyle='--', label='link capacity')
    plt.xlabel('offered rate (Mbps)')
    plt.ylabel('measured rate (Mbps)')
    plt.title('UDP app vs SDN flow statistics')
    plt.grid(alpha=0.3)
    plt.legend()
    plt.tight_layout()
    plt.savefig(os.path.join(a.results, 'throughput.png'), dpi=150)
    plt.close()

    fig, ax = plt.subplots(1, 2, figsize=(11, 4))
    ax[0].plot(summ.offered_mbps, summ.app_loss_pct, 'o-', label='app (sent - received)')
    ax[0].plot(summ.offered_mbps, summ.sdn_loss_pct, 's--', label='SDN (s1 pkts - s2 pkts)')
    ax[0].set_xlabel('offered rate (Mbps)')
    ax[0].set_ylabel('loss (%)')
    ax[0].set_title('packet loss')
    ax[0].grid(alpha=0.3)
    ax[0].legend()
    ax[1].plot(summ.offered_mbps, summ.jitter_ms, 'o-', color='tab:green')
    ax[1].set_xlabel('offered rate (Mbps)')
    ax[1].set_ylabel('jitter (ms)')
    ax[1].set_title('jitter (RFC 3550)')
    ax[1].grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(os.path.join(a.results, 'loss_jitter.png'), dpi=150)
    plt.close()

    if a.ts_rate is None:
        pick = summ.iloc[-1]
    else:
        pick = summ.iloc[(summ.offered_mbps - a.ts_rate).abs().argmin()]
    srv_path = os.path.join(a.results, 'server_%d.csv' % int(pick['run']))
    if os.path.exists(srv_path):
        srv = pd.read_csv(srv_path)
        ff = flows[flows.udp_dst == pick['port']]
        t0 = min(srv.epoch_s.min(), ff.epoch_s.min())
        plt.figure(figsize=(8, 4.5))
        plt.plot(srv.epoch_s - t0, srv.rx_mbps, 'o-', label='app rx (payload)')
        for dpid, lab in ((1, 'SDN s1'), (2, 'SDN s2')):
            g = ff[ff.dpid == dpid].sort_values('epoch_s')
            plt.plot(g.epoch_s - t0, g.sdn_mbps, 's--', label=lab + ' (frame bytes)')
        plt.xlabel('time (s)')
        plt.ylabel('rate per interval (Mbps)')
        plt.title('per-interval bandwidth at %g Mbps offered' % pick['offered_mbps'])
        plt.grid(alpha=0.3)
        plt.legend()
        plt.tight_layout()
        plt.savefig(os.path.join(a.results, 'timeseries.png'), dpi=150)
        plt.close()


if __name__ == '__main__':
    main()
