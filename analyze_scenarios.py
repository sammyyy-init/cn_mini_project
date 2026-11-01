import argparse
import os

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd

ALERTS = ('CONGESTION', 'PACKET_LOSS', 'LINK_FAILURE')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--results', default='results')
    a = ap.parse_args()

    scn = pd.read_csv(os.path.join(a.results, 'scenarios.csv'))
    flows = pd.read_csv(os.path.join(a.results, 'sdn_flows.csv'))
    ev_path = os.path.join(a.results, 'sdn_events.csv')
    events = pd.read_csv(ev_path) if os.path.exists(ev_path) else pd.DataFrame(
        columns=['epoch_s', 'event', 'dpid', 'udp_dst', 'detail'])

    rows = []
    for _, s in scn.iterrows():
        t0, t1 = s['start_epoch'], s['end_epoch']
        srv_path = os.path.join(a.results, 'scn_%s_server.csv' % s['name'])
        srv = pd.read_csv(srv_path) if os.path.exists(srv_path) else None
        ff = flows[(flows.udp_dst == s['port']) & (flows.epoch_s >= t0 - 1) & (flows.epoch_s <= t1 + 5)]
        ev = events[(events.epoch_s >= t0 - 1) & (events.epoch_s <= t1 + 5)]

        alerts = ev[ev.event.isin(ALERTS) & (ev.udp_dst == s['port'])]
        first_alert = alerts.iloc[0] if len(alerts) else None
        link_down = ev[ev.event == 'LINK_DOWN']
        recovered = ev[(ev.event == 'RECOVERED') & (ev.udp_dst == s['port'])]

        loss = (s['tx_pkts'] - s['rx_pkts']) / s['tx_pkts'] * 100 if s['tx_pkts'] else 0.0
        rec = ''
        if pd.notna(s['up_epoch']) and srv is not None:
            after = srv[(srv.epoch_s > s['up_epoch']) & (srv.rx_mbps >= 0.8 * s['offered_mbps'])]
            if len(after):
                rec = round(after.epoch_s.min() - s['up_epoch'], 2)
        ref = s['down_epoch'] if pd.notna(s['down_epoch']) else t0
        rows.append({
            'scenario': s['name'],
            'offered_mbps': s['offered_mbps'],
            'link_loss_cfg_pct': s['loss_cfg_pct'],
            'app_loss_pct': round(loss, 2),
            'jitter_ms': s['jitter_ms'],
            'first_alert': first_alert['event'] if first_alert is not None else '',
            'alert_delay_s': round(first_alert['epoch_s'] - ref, 2) if first_alert is not None else '',
            'link_down_event': 'yes' if len(link_down) else '',
            'recovered_event': 'yes' if len(recovered) else '',
            'traffic_recovery_s': rec,
        })

        plt.figure(figsize=(9, 4.5))
        if srv is not None:
            plt.plot(srv.epoch_s - t0, srv.rx_mbps, 'o-', label='app rx (payload)')
        for dpid, lab in ((1, 'SDN s1'), (2, 'SDN s2')):
            g = ff[ff.dpid == dpid].sort_values('epoch_s')
            plt.plot(g.epoch_s - t0, g.sdn_mbps, 's--', label=lab + ' (frame bytes)')
        if pd.notna(s['down_epoch']) and pd.notna(s['up_epoch']):
            plt.axvspan(s['down_epoch'] - t0, s['up_epoch'] - t0, color='red', alpha=0.12, label='link down')
        for _, e in ev.iterrows():
            if e.event in ('LINK_DOWN', 'LINK_UP') or e.udp_dst == s['port']:
                plt.axvline(e.epoch_s - t0, color='gray', linestyle=':')
                plt.text(e.epoch_s - t0, plt.ylim()[1] * 0.95, e.event, rotation=90,
                         fontsize=7, va='top', ha='right')
        plt.xlabel('time since traffic start (s)')
        plt.ylabel('rate per interval (Mbps)')
        plt.title('scenario: %s (%g Mbps offered)' % (s['name'], s['offered_mbps']))
        plt.grid(alpha=0.3)
        plt.legend(fontsize=8)
        plt.tight_layout()
        plt.savefig(os.path.join(a.results, 'scenario_%s.png' % s['name']), dpi=150)
        plt.close()

    out = pd.DataFrame(rows)
    out.to_csv(os.path.join(a.results, 'scenario_summary.csv'), index=False)
    print(out.to_string(index=False))


if __name__ == '__main__':
    main()
