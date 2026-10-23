/* SPDX-License-Identifier: GPL-2.0 */
#include <linux/bpf.h>
#include <linux/pkt_cls.h>
#include <linux/if_ether.h>
#include <linux/ip.h>
#include <linux/tcp.h>

#define SEC(name) __attribute__((section(name), used))
#define __uint(name, val) int (*name)[val]
#define __type(name, val) typeof(val) *name

static void *(*bpf_map_lookup_elem)(void *map, const void *key) = (void *)BPF_FUNC_map_lookup_elem;

struct oe_net_cfg {
	__u32 target_port;
	__u32 drop_target;
};

struct oe_net_stats {
	__u64 total_packets;
	__u64 total_bytes;
	__u64 target_packets;
	__u64 target_bytes;
	__u64 dropped_packets;
};

struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, struct oe_net_cfg);
} oe_net_cfg_map SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, struct oe_net_stats);
} oe_net_stats_map SEC(".maps");

SEC("classifier")
int oe_tc_cls(struct __sk_buff *skb)
{
	__u32 key = 0;
	struct oe_net_stats *stats = bpf_map_lookup_elem(&oe_net_stats_map, &key);
	struct oe_net_cfg *cfg = bpf_map_lookup_elem(&oe_net_cfg_map, &key);

	if (!stats)
		return TC_ACT_OK;

	__sync_fetch_and_add(&stats->total_packets, 1);
	__sync_fetch_and_add(&stats->total_bytes, skb->len);

	void *data = (void *)(long)skb->data;
	void *data_end = (void *)(long)skb->data_end;
	struct ethhdr *eth = data;

	if ((void *)(eth + 1) > data_end)
		return TC_ACT_OK;
	if (eth->h_proto != __constant_htons(ETH_P_IP))
		return TC_ACT_OK;

	struct iphdr *ip = (void *)(eth + 1);
	if ((void *)(ip + 1) > data_end)
		return TC_ACT_OK;
	if (ip->protocol != 6)
		return TC_ACT_OK;

	int ip_hl = ip->ihl * 4;
	if (ip_hl < (int)sizeof(*ip) || ip_hl > 60)
		return TC_ACT_OK;

	struct tcphdr *tcp = (void *)ip + ip_hl;
	if ((void *)(tcp + 1) > data_end)
		return TC_ACT_OK;

	__u16 dport = __builtin_bswap16(tcp->dest);
	__u16 sport = __builtin_bswap16(tcp->source);

	if (cfg && cfg->target_port > 0 && (dport == cfg->target_port || sport == cfg->target_port)) {
		__sync_fetch_and_add(&stats->target_packets, 1);
		__sync_fetch_and_add(&stats->target_bytes, skb->len);
		if (cfg->drop_target && dport == cfg->target_port) {
			__sync_fetch_and_add(&stats->dropped_packets, 1);
			return TC_ACT_SHOT;
		}
	}
	return TC_ACT_OK;
}

char _license[] SEC("license") = "GPL";
