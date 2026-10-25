#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>
#include <pthread.h>
#include <endian.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include "common.h"

typedef struct {
    int id, port;
    uint64_t rx_pkts, rx_bytes, delay_ns, jitter_ns;   /* updated by receiver thread */
    uint32_t next_seq;                      /* highest seq seen + 1 */
    uint64_t last_pkts, last_bytes, last_delay, last_expected;  /* reporter only */
} flow_t;

static flow_t flows[MAX_FLOWS];
static volatile sig_atomic_t stop_flag = 0;
static void on_sig(int s) { (void)s; stop_flag = 1; }

static void *receiver(void *arg)
{
    flow_t *f = arg;
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) { perror("socket"); return NULL; }
    int one = 1, rcvbuf = 4 * 1024 * 1024;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &rcvbuf, sizeof rcvbuf);
    struct timeval tv = { 0, 200000 };      /* wake up to check stop flag */
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);

    struct sockaddr_in a = {0};
    a.sin_family = AF_INET;
    a.sin_addr.s_addr = htonl(INADDR_ANY);
    a.sin_port = htons(f->port);
    if (bind(fd, (struct sockaddr *)&a, sizeof a) < 0) { perror("bind"); close(fd); return NULL; }

    char buf[2048];
    uint64_t prev_t = 0, prev_ts = 0;
    double jit = 0.0;
    while (!stop_flag) {
        ssize_t n = recvfrom(fd, buf, sizeof buf, 0, NULL, NULL);
        if (n < HDR_SIZE) continue;         /* timeout or runt packet */
        uint64_t t = now_ns(CLOCK_REALTIME);
        struct pkt_hdr *h = (struct pkt_hdr *)buf;
        uint32_t seq = ntohl(h->seq);
        uint64_t ts = be64toh(h->ts_ns);
        __atomic_fetch_add(&f->rx_pkts, 1, __ATOMIC_RELAXED);
        __atomic_fetch_add(&f->rx_bytes, (uint64_t)n, __ATOMIC_RELAXED);
        __atomic_fetch_add(&f->delay_ns, t > ts ? t - ts : 0, __ATOMIC_RELAXED);
        if (prev_t) {
            int64_t d = (int64_t)(t - prev_t) - (int64_t)(ts - prev_ts);
            int64_t ad = d < 0 ? -d : d;
            jit += ((double)ad - jit) / 16.0;
            __atomic_store_n(&f->jitter_ns, (uint64_t)jit, __ATOMIC_RELAXED);
        }
        prev_t = t;
        prev_ts = ts;
        if (seq + 1 > f->next_seq)          /* only this thread writes it */
            __atomic_store_n(&f->next_seq, seq + 1, __ATOMIC_RELAXED);
    }
    close(fd);
    return NULL;
}

int main(int argc, char **argv)
{
    int base_port = 5000, nflows = 1, duration = 0, opt;
    const char *out = "server.csv";
    while ((opt = getopt(argc, argv, "p:n:t:o:")) != -1) {
        switch (opt) {
        case 'p': base_port = atoi(optarg); break;
        case 'n': nflows = atoi(optarg); break;
        case 't': duration = atoi(optarg); break;
        case 'o': out = optarg; break;
        default:
            fprintf(stderr, "usage: %s [-p base_port] [-n flows] [-t seconds(0=until Ctrl+C)] [-o csv]\n", argv[0]);
            return 1;
        }
    }
    if (nflows < 1 || nflows > MAX_FLOWS) { fprintf(stderr, "flows must be 1..%d\n", MAX_FLOWS); return 1; }

    signal(SIGINT, on_sig);
    signal(SIGTERM, on_sig);

    FILE *csv = fopen(out, "w");
    if (!csv) { perror("fopen"); return 1; }
    fprintf(csv, "epoch_s,flow_id,rx_pkts,rx_bytes,rx_mbps,lost_pkts,loss_pct,avg_delay_ms,jitter_ms\n");

    pthread_t th[MAX_FLOWS];
    for (int i = 0; i < nflows; i++) {
        flows[i].id = i;
        flows[i].port = base_port + i;
        pthread_create(&th[i], NULL, receiver, &flows[i]);
    }
    fprintf(stderr, "server: %d flow(s) listening on UDP %d..%d\n", nflows, base_port, base_port + nflows - 1);

    uint64_t t0 = now_ns(CLOCK_MONOTONIC), last = t0;
    while (!stop_flag) {
        struct timespec ts = { 1, 0 };
        nanosleep(&ts, NULL);
        uint64_t now = now_ns(CLOCK_MONOTONIC);
        double dt = (now - last) / 1e9;
        last = now;
        double epoch = now_ns(CLOCK_REALTIME) / 1e9;

        for (int i = 0; i < nflows; i++) {
            flow_t *f = &flows[i];
            uint64_t pk = __atomic_load_n(&f->rx_pkts, __ATOMIC_RELAXED);
            uint64_t by = __atomic_load_n(&f->rx_bytes, __ATOMIC_RELAXED);
            uint64_t dl = __atomic_load_n(&f->delay_ns, __ATOMIC_RELAXED);
            uint64_t ex = __atomic_load_n(&f->next_seq, __ATOMIC_RELAXED);
            if (pk == 0) continue;          /* flow not started yet */
            uint64_t dpk = pk - f->last_pkts, dby = by - f->last_bytes;
            uint64_t ddl = dl - f->last_delay, dex = ex - f->last_expected;
            int64_t lost = (int64_t)dex - (int64_t)dpk;
            if (lost < 0) lost = 0;
            fprintf(csv, "%.3f,%d,%llu,%llu,%.4f,%lld,%.2f,%.3f,%.4f\n", epoch, f->id,
                    (unsigned long long)dpk, (unsigned long long)dby,
                    dby * 8.0 / dt / 1e6, (long long)lost,
                    dex ? 100.0 * lost / dex : 0.0,
                    dpk ? ddl / 1e6 / dpk : 0.0,
                    __atomic_load_n(&f->jitter_ns, __ATOMIC_RELAXED) / 1e6);
            f->last_pkts = pk; f->last_bytes = by; f->last_delay = dl; f->last_expected = ex;
        }
        fflush(csv);
        if (duration && (now - t0) / 1e9 >= duration) stop_flag = 1;
    }

    for (int i = 0; i < nflows; i++) pthread_join(th[i], NULL);
    fprintf(stderr, "\n--- server summary ---\n");
    for (int i = 0; i < nflows; i++) {
        flow_t *f = &flows[i];
        if (!f->rx_pkts) continue;
        double loss = f->next_seq ? 100.0 * ((double)f->next_seq - f->rx_pkts) / f->next_seq : 0;
        fprintf(stderr, "flow %d: rx=%llu pkts, %llu bytes, expected=%u, loss=%.2f%%, jitter=%.4f ms, delay=%.4f ms\n", f->id,
                (unsigned long long)f->rx_pkts, (unsigned long long)f->rx_bytes, f->next_seq, loss,
                f->jitter_ns / 1e6, f->delay_ns / 1e6 / f->rx_pkts);
    }
    fclose(csv);
    return 0;
}
