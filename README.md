# UDP Traffic Measurement vs SDN Flow Statistics

Computer Networks mini project. We send paced UDP traffic through an emulated SDN network and measure it at two places: at the application (UDP client/server) and inside the network (OpenFlow flow statistics polled by a Ryu controller). Comparing the two shows how much overhead, queueing and loss the application does not see on its own.

## What it measures

| Layer | Where | What is counted | Metrics |
|---|---|---|---|
| Application | UDP client and server (C) | UDP payload only | throughput, loss, delay, jitter |
| Network (SDN) | OpenFlow switches s1 and s2, polled by Ryu | whole Ethernet frame (payload + 42 bytes of Ethernet, IP and UDP headers) | per-flow bandwidth, packet counts, loss between switches |

The two numbers differ because the switch counts 42 extra header bytes per packet. Two measurement points are used because s1 sits before the bottleneck link (it sees what was offered) and s2 sits after it (it sees what was delivered). The difference between them is traffic dropped at the bottleneck.

## Architecture

```
h1 (UDP client) --100M-- s1 ====== bottleneck ====== s2 --100M-- h3 (UDP server)
h2 (background) --100M--/   10 Mbps, 5 ms, queue 100   \--100M-- h4 (background sink)

                 s1, s2  <---- OpenFlow 1.3 ---->  Ryu controller
                                                    - l2_switch.py: learning switch + per-UDP-flow rules
                                                    - stats_monitor.py: polls flow/port stats every 1 s
```

- The bottleneck link is shaped with `tc` through Mininet (`--bw`, `--delay`, `--loss`, `--queue`).
- `l2_switch.py` installs one rule per UDP flow, matching (source IP, destination IP, UDP destination port), so the switch keeps separate byte and packet counters for each test run.
- `stats_monitor.py` sends flow and port stats requests every second and writes bandwidth from the change in `byte_count` between two polls.
- Each offered rate in a sweep uses its own UDP port, so each run has its own flow rule and clean counters.

## Repository layout

```
cn_mini_project/
├── controller/
│   ├── l2_switch.py        learning switch, per-UDP-flow rules
│   └── stats_monitor.py    flow and port statistics polling, writes CSVs
├── topology/
│   └── topo.py             standalone Mininet topology with CLI for manual testing
├── udp_app/
│   ├── client.c            paced UDP sender
│   ├── server.c            receiver with throughput, loss, delay and jitter
│   ├── common.h            packet header and constants
│   └── Makefile
├── run_experiment.py       builds the network and runs the full rate sweep
├── analyze.py              merges app and SDN results, makes the table and graphs
├── README.md
└── .gitignore
```

## UDP application

Packet format: a 16-byte header followed by padding up to the chosen payload size (`-l`, default 1200 in the app, 1000 in the experiment).

| Field | Size | Use |
|---|---|---|
| flow_id | 4 bytes | which flow |
| seq | 4 bytes | loss and reordering |
| timestamp | 8 bytes | one-way delay and jitter |

The client sends at a fixed rate by scheduling each packet at `payload * 8 / rate` intervals. The payload size includes the 16-byte header, so a 1000-byte payload becomes a 1042-byte Ethernet frame.

Client options: `-s server_ip  -p base_port  -l payload_bytes  -r rate_mbps  -t seconds  -n flows  -o csv`

Server options: `-p base_port  -n flows  -t seconds  -o csv`

## How the metrics are computed

- Throughput (app): `received_bytes * 8 / duration`
- Throughput (SDN): `byte_count * 8 / duration` for the flow rule at s1 and at s2
- Loss (app): `(packets_sent - packets_received) / packets_sent`
- Loss (SDN): `(packets_at_s1 - packets_at_s2) / packets_at_s1`
- Delay: receive time minus the sender timestamp (client and server share one clock inside Mininet)
- Jitter: RFC 3550 running estimate, `J = J + (|D| - J) / 16`, where `D` is the change in delay between consecutive packets
- Overhead check: `byte_count / packet_count - payload` should come out at 42

The SDN bandwidth formula used by the controller:

```
bandwidth = (byte_count_now - byte_count_previous) * 8 / (duration_now - duration_previous)
```

## Requirements

- Linux (Ubuntu 22.04 or 24.04 in a VM, or WSL2). Mininet and Open vSwitch do not run on native Windows.
- Mininet, Open vSwitch, gcc and make
- Python 3.9 environment for Ryu 4.34 (it does not work on newer Python versions)
- pandas and matplotlib for the analysis

## Setup

System packages:

```
sudo apt update
sudo apt install -y mininet openvswitch-switch build-essential python3-pandas python3-matplotlib curl git
```

Ryu in its own Python 3.9 environment:

```
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.local/bin/env
uv venv --seed --python 3.9 ~/ryuenv
source ~/ryuenv/bin/activate
pip install "setuptools==58.2.0" wheel "eventlet==0.30.2"
pip install --no-build-isolation --no-deps "ryu==4.34"
pip install msgpack netaddr oslo.config routes six tinyrpc webob ovs
ryu-manager --version
```

Build the UDP app:

```
make -C udp_app
```

## Running the experiment

Always run from the repository root.

1. Terminal 1, start the controller and leave it running. Wait for the `stats monitor: polling every 1.0s` line.

```
source ~/ryuenv/bin/activate
rm -rf results
ryu-manager controller/l2_switch.py controller/stats_monitor.py
```

2. Terminal 2, clean old Mininet state and run the sweep (about 2.5 minutes with the defaults).

```
sudo mn -c
sudo python3 run_experiment.py
```

3. When it finishes, press Ctrl+C in Terminal 1 so the CSVs are flushed, then analyze.

```
python3 analyze.py
```

Restart Ryu before every run. `stats_monitor.py` starts a fresh `sdn_flows.csv` each time, and the UDP results must come from the same session.

Quick test: `sudo python3 run_experiment.py --rates 2,12 --duration 5`

### Options for run_experiment.py

| Option | Default | Meaning |
|---|---|---|
| `--bw` | 10 | bottleneck bandwidth in Mbps |
| `--delay` | 5ms | bottleneck delay |
| `--loss` | 0 | random loss on the bottleneck in percent |
| `--queue` | 100 | bottleneck queue length in packets |
| `--rates` | 1,2,4,6,8,9,10,12,15 | offered rates to sweep in Mbps |
| `--size` | 1000 | UDP payload size in bytes |
| `--duration` | 10 | seconds per run |
| `--bg` | 0 | background traffic h2 to h4 in Mbps |
| `--settle` | 2.5 | seconds to wait after each run so the last stats poll lands |

`analyze.py` options: `--results results`, `--bw 10` (draws the link capacity line), `--ts-rate N` (picks which run the time-series graph shows).

## Output files

Everything is written to `results/`.

| File | Written by | Contents |
|---|---|---|
| `udp_summary.csv` | run_experiment.py | totals per run: packets and bytes sent and received, jitter, mean delay |
| `client_N.csv`, `server_N.csv` | UDP app | per-second logs for run N |
| `sdn_flows.csv` | stats_monitor.py | per-flow byte and packet counters at s1 and s2, with bandwidth per poll |
| `sdn_ports.csv` | stats_monitor.py | per-port rx and tx rates and drop counters |
| `summary.csv` | analyze.py | one row per rate with app and SDN throughput, loss, frame size, overhead, jitter |
| `throughput.png` | analyze.py | offered rate vs app, s1 and s2 throughput with the link capacity line |
| `loss_jitter.png` | analyze.py | loss (app and SDN) and jitter vs offered rate |
| `timeseries.png` | analyze.py | rate over time for one run, app vs s1 vs s2 |
