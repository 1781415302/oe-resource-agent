"""Relative weighting, no absolute SLO veto, honest missing-data protection."""
from dataclasses import fields
import unittest
from test_live_agent import observation, propose
from oeagent.weighted_agent import AgentWeights, Timing, WeightedAgent, weighted_score


def weighted_proposal(agent, **changes):
    for seq in range(20,26):
        window=dict(hdr_id="baseline",hdr_start=13,hdr_end=18,hdr_p99_ms=10,frontend_rps=1000,
                    completion_fraction=1,request_errors=0) if changes.get("frontend_present",True) else {}
        action=agent.observe(observation(seq,**window,**changes))
        if action:return action


class WeightedChecks(unittest.TestCase):
    def test_reference_is_100_and_units_cancel(self):
        ref=dict(frontend_present=True,background_present=True,hdr_p99_ms=8,eps=1000,frontend_rps=100)
        self.assertAlmostEqual(weighted_score(ref,ref,AgentWeights()),100)
        new=ref|dict(hdr_p99_ms=4,eps=900)
        self.assertAlmostEqual(weighted_score(ref,new,AgentWeights()),100*2**.6*.9**.2)
        self.assertAlmostEqual(weighted_score(ref,new,AgentWeights(60,40)),weighted_score(ref,new,AgentWeights(.6,.4)))

    def test_changing_weights_changes_rank(self):
        ref=dict(frontend_present=True,background_present=True,hdr_p99_ms=8,eps=1000)
        fast=ref|dict(hdr_p99_ms=2,eps=600)
        productive=ref|dict(hdr_p99_ms=5,eps=1200)
        self.assertGreater(weighted_score(ref,fast,AgentWeights(.8,.2)),weighted_score(ref,productive,AgentWeights(.8,.2)))
        self.assertLess(weighted_score(ref,fast,AgentWeights(.2,.8)),weighted_score(ref,productive,AgentWeights(.2,.8)))

    def test_missing_reports_are_not_zero_or_infinite_benefit(self):
        ref=dict(frontend_present=True,background_present=True,hdr_p99_ms=8,eps=1000)
        self.assertIsNone(weighted_score(ref,ref|dict(eps=None),AgentWeights()))
        self.assertIsNone(weighted_score(ref,ref|dict(hdr_p99_ms=None),AgentWeights()))
        self.assertLess(weighted_score(ref,ref|dict(eps=0),AgentWeights()),1)

    def test_pure_background_uses_available_throughput(self):
        ref=dict(frontend_present=False,background_present=True,hdr_p99_ms=None,eps=1000)
        self.assertAlmostEqual(weighted_score(ref,ref|dict(eps=1100),AgentWeights()),110)
        action=weighted_proposal(WeightedAgent(),frontend_present=False,frontend_cpu_pct=0,background_cpu_pct=95,
                       background_runnable=4,wait_pct=None,avg_wait_us=None,probe_p95_ms=None)
        self.assertEqual(action["strategy_id"],"scx-10ms")

    def test_candidate_above_old_latency_limit_and_below_old_eps_floor_can_win(self):
        agent=WeightedAgent()
        action=weighted_proposal(agent)
        agent.committed(action,25,"default")
        for seq in (28,31,34):
            window=dict(hdr_id="fresh",hdr_start=27,hdr_end=32,hdr_p99_ms=7,frontend_rps=1000,
                        completion_fraction=.9,request_errors=0) if seq==34 else {}
            result=agent.observe(observation(seq,strategy="scx-1ms",probe_p95_ms=2,eps=700,**window))
        self.assertIsNone(result)
        self.assertEqual(agent.phase,"accepted")
        self.assertGreater(agent.events[-1]["weighted_score"],100)
        self.assertNotIn("latency_ms",{f.name for f in fields(Timing)})
        self.assertIsNone(agent.status()["performance_targets"])

    def test_lower_score_rolls_back(self):
        agent=WeightedAgent()
        action=weighted_proposal(agent);agent.committed(action,25,"default")
        for seq in (28,31,34):
            window=dict(hdr_id="fresh",hdr_start=27,hdr_end=32,hdr_p99_ms=12,frontend_rps=1000,
                        completion_fraction=1,request_errors=0) if seq==34 else {}
            result=agent.observe(observation(seq,strategy="scx-1ms",probe_p95_ms=9,eps=900,**window))
        self.assertEqual(result["kind"],"rollback")
        self.assertEqual(result["strategy_id"],"default")

    def test_request_errors_still_trigger_protection(self):
        agent=WeightedAgent()
        agent.observe(observation(1,strategy="scx-1ms",probe_errors=1))
        result=agent.observe(observation(2,strategy="scx-1ms",probe_errors=1))
        self.assertEqual(result["strategy_id"],"default")

    def test_weights_reject_invalid_values(self):
        for weights in ((0,0),(-1,1),(float("nan"),1),(1,float("inf"))):
            with self.assertRaises(ValueError):AgentWeights(*weights)


if __name__=="__main__":unittest.main()
