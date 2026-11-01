import os
import time

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, DEAD_DISPATCHER, set_ev_cls
from ryu.lib import hub
from ryu.ofproto import ofproto_v1_3

POLL = float(os.environ.get('GUARD_POLL', '1'))
LOSS_PCT = float(os.environ.get('GUARD_LOSS_PCT', '3'))
MIN_LOST = int(os.environ.get('GUARD_MIN_LOST', '20'))
CONFIRM = int(os.environ.get('GUARD_CONFIRM', '2'))
LINK_MBPS = float(os.environ.get('GUARD_LINK_MBPS', '10'))
MITIGATE = os.environ.get('GUARD_MITIGATE', '0') == '1'
LIMIT_KBPS = int(os.environ.get('GUARD_LIMIT_KBPS', '9000'))
EVENT_CSV = os.environ.get('SDN_EVENT_CSV', 'results/sdn_events.csv')
INGRESS_DPID = 1
EGRESS_DPID = 2


def udp_port(match):
    try:
        return match['udp_dst']
    except Exception:
        return None


class CongestionGuard(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.datapaths = {}
        self.stats = {}
        self.prev = {}
        self.bad = {}
        self.good = {}
        self.active = set()
        self.meters = {}
        self.down_ports = set()
        os.makedirs(os.path.dirname(EVENT_CSV) or '.', exist_ok=True)
        self.ev_f = open(EVENT_CSV, 'w')
        self.ev_f.write('epoch_s,event,dpid,udp_dst,detail\n')
        self.ev_f.flush()
        self.poller = hub.spawn(self._loop)
        self.logger.info('congestion guard: loss>=%.1f%% for %d polls, mitigation %s -> %s',
                         LOSS_PCT, CONFIRM, 'on' if MITIGATE else 'off', EVENT_CSV)

    def _event(self, name, dpid, port, detail):
        self.ev_f.write('%.3f,%s,%s,%s,%s\n' % (time.time(), name, dpid, port, detail))
        self.ev_f.flush()
        self.logger.warning('%s dpid=%s udp_dst=%s %s', name, dpid, port, detail)

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change(self, ev):
        dp = ev.datapath
        self.down_ports = {k for k in self.down_ports if k[0] != dp.id}
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dp.id] = dp
        else:
            self.datapaths.pop(dp.id, None)

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def _port_status(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp = dp.ofproto
        port = msg.desc.port_no
        if port >= ofp.OFPP_MAX:
            return
        down = bool(msg.desc.state & ofp.OFPPS_LINK_DOWN) or bool(msg.desc.config & ofp.OFPPC_PORT_DOWN)
        key = (dp.id, port)
        if down and key not in self.down_ports:
            self.down_ports.add(key)
            self._event('LINK_DOWN', dp.id, '', 'port=%d' % port)
        elif not down and key in self.down_ports:
            self.down_ports.discard(key)
            self._event('LINK_UP', dp.id, '', 'port=%d' % port)

    def _loop(self):
        while True:
            for dp in list(self.datapaths.values()):
                dp.send_msg(dp.ofproto_parser.OFPFlowStatsRequest(dp))
            hub.sleep(POLL * 0.5)
            self._evaluate()
            hub.sleep(POLL * 0.5)

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def _flow_reply(self, ev):
        dpid = ev.msg.datapath.id
        for st in ev.msg.body:
            port = udp_port(st.match)
            if port is not None:
                self.stats[(dpid, port)] = st

    def _classify(self, offered_mbps):
        if self.down_ports:
            return 'LINK_FAILURE'
        if offered_mbps >= LINK_MBPS * 0.95:
            return 'CONGESTION'
        return 'PACKET_LOSS'

    def _evaluate(self):
        for port in {p for (_, p) in self.stats}:
            a = self.stats.get((INGRESS_DPID, port))
            b = self.stats.get((EGRESS_DPID, port))
            if a is None or b is None:
                continue
            now = time.time()
            cur = (a.packet_count, b.packet_count, a.byte_count, now)
            old = self.prev.get(port)
            self.prev[port] = cur
            if old is None:
                continue
            d_in = cur[0] - old[0]
            d_out = cur[1] - old[1]
            if d_in < 0 or d_out < 0:
                continue
            lost = max(d_in - d_out, 0)
            pct = 100.0 * lost / d_in if d_in > 0 else 0.0
            offered = (cur[2] - old[2]) * 8.0 / max(now - old[3], 1e-6) / 1e6
            if pct >= LOSS_PCT and lost >= MIN_LOST:
                self.bad[port] = self.bad.get(port, 0) + 1
                self.good[port] = 0
                if self.bad[port] >= CONFIRM and port not in self.active:
                    self.active.add(port)
                    self._event(self._classify(offered), INGRESS_DPID, port,
                                'loss=%.1f%% lost=%d of %d pkts offered=%.2f Mbps' % (pct, lost, d_in, offered))
                    if MITIGATE and not self.down_ports:
                        self._police(port, a)
            else:
                self.good[port] = self.good.get(port, 0) + 1
                self.bad[port] = 0
                if port in self.active and self.good[port] >= CONFIRM:
                    self.active.discard(port)
                    self._event('RECOVERED', INGRESS_DPID, port, 'loss=%.1f%%' % pct)

    def _police(self, port, st):
        dp = self.datapaths.get(INGRESS_DPID)
        if dp is None or port in self.meters:
            return
        p, ofp = dp.ofproto_parser, dp.ofproto
        mid = len(self.meters) + 1
        self.meters[port] = mid
        dp.send_msg(p.OFPMeterMod(datapath=dp, command=ofp.OFPMC_ADD, flags=ofp.OFPMF_KBPS,
                                  meter_id=mid,
                                  bands=[p.OFPMeterBandDrop(rate=LIMIT_KBPS, burst_size=100)]))
        inst = [p.OFPInstructionMeter(mid)] + list(st.instructions)
        dp.send_msg(p.OFPFlowMod(datapath=dp, command=ofp.OFPFC_MODIFY_STRICT,
                                 priority=st.priority, match=st.match,
                                 instructions=inst, table_id=st.table_id))
        self._event('POLICER_INSTALLED', INGRESS_DPID, port, 'limit=%d kbps' % LIMIT_KBPS)
