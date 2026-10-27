"""Relative weighting, no absolute SLO veto, honest missing-data protection."""
from dataclasses import fields
import unittest
from test_live_agent import observation, propose
from oeagent.demo_tuning import scx_strategy_id, strategy_spec
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
        target=action["strategy_id"]
        agent.committed(action,25,"default")
        for seq in (28,31,34):
            window=dict(hdr_id="fresh",hdr_start=27,hdr_end=32,hdr_p99_ms=7,frontend_rps=1000,
                        completion_fraction=.9,request_errors=0) if seq==34 else {}
            result=agent.observe(observation(seq,strategy=target,probe_p95_ms=2,eps=700,**window))
        self.assertIsNone(result)
        self.assertEqual(agent.phase,"accepted")
        self.assertGreater(agent.events[-1]["weighted_score"],100)
        self.assertNotIn("latency_ms",{f.name for f in fields(Timing)})
        self.assertIsNone(agent.status()["performance_targets"])

    def test_lower_score_rolls_back(self):
        agent=WeightedAgent()
        action=weighted_proposal(agent);target=action["strategy_id"]
        agent.committed(action,25,"default")
        for seq in (28,31,34):
            window=dict(hdr_id="fresh",hdr_start=27,hdr_end=32,hdr_p99_ms=12,frontend_rps=1000,
                        completion_fraction=1,request_errors=0) if seq==34 else {}
            result=agent.observe(observation(seq,strategy=target,probe_p95_ms=9,eps=900,**window))
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


class SliceSearchChecks(unittest.TestCase):
    """The Agent computes a slice value; it does not pick from a fixed menu."""

    def test_first_probe_is_derived_from_the_measured_queueing(self):
        agent=WeightedAgent()
        action=weighted_proposal(agent,avg_wait_us=2400,background_runnable=4)
        self.assertEqual(action["strategy_id"],"scx-u600p1")   # 2400 / 4 = 600
        self.assertEqual(strategy_spec(action["strategy_id"])["slice_us"],600)
        self.assertEqual(agent.search["points"],[])

    def test_first_probe_is_frozen_while_the_wait_measurement_jitters(self):
        agent=WeightedAgent()
        proposals=set()
        for index,wait in enumerate((2400,2860,2750,2920,2810)):
            window=dict(hdr_id="baseline",hdr_start=13,hdr_end=18,hdr_p99_ms=10,frontend_rps=1000,
                        completion_fraction=1,request_errors=0)
            agent.observe(observation(20+index,avg_wait_us=wait,**window))
            if agent.search["first"] is not None:
                proposals.add(agent.search["first"])
        # Median of the first three waits is 2750us; 2750/4 -> 700us, then frozen
        # so the confirmation counter is not reset by every jittering sample.
        self.assertEqual(proposals,{"scx-u700p1"})

    def test_no_frontend_starts_from_a_long_background_slice(self):
        agent=WeightedAgent()
        action=weighted_proposal(agent,frontend_present=False,avg_wait_us=None,background_runnable=4)
        self.assertEqual(action["strategy_id"],"scx-10ms")

    def test_second_probe_halves_the_slice_when_the_first_point_won(self):
        agent=WeightedAgent()
        first=weighted_proposal(agent,avg_wait_us=2400,background_runnable=4)
        agent.committed(first,25,"default")
        for seq in (28,31,34):
            window=dict(hdr_id="fresh",hdr_start=27,hdr_end=32,hdr_p99_ms=7,frontend_rps=1000,
                        completion_fraction=1,request_errors=0) if seq==34 else {}
            agent.observe(observation(seq,strategy=first["strategy_id"],probe_p95_ms=2,eps=700,**window))
        self.assertEqual(agent.phase,"accepted")
        second=None
        for seq in range(35,42):
            second=second or agent.observe(observation(seq))
        # 600us scored >100, so the remaining probe halves the slice: 300us, preemption kept.
        self.assertEqual(second["strategy_id"],"scx-u300p1")
        self.assertTrue(strategy_spec(second["strategy_id"])["wakeup_preempt"])
        self.assertEqual(agent.search["points"][0]["slice_us"],600)

    def test_second_probe_flips_preemption_when_the_first_point_lost(self):
        agent=WeightedAgent()
        first=weighted_proposal(agent,avg_wait_us=2400,background_runnable=4)
        agent.committed(first,25,"default")
        action=None
        for seq in (28,31,34):
            window=dict(hdr_id="fresh",hdr_start=27,hdr_end=32,hdr_p99_ms=20,frontend_rps=1000,
                        completion_fraction=1,request_errors=0) if seq==34 else {}
            action=agent.observe(observation(seq,strategy=first["strategy_id"],probe_p95_ms=9,eps=900,**window)) or action
        self.assertEqual(action["kind"],"rollback")
        agent.committed(action,34,first["strategy_id"])
        second=None
        for seq in range(35,44):
            second=second or agent.observe(observation(seq,avg_wait_us=2400))
        # The slice direction did not help, so the remaining probe tests preemption instead.
        self.assertEqual(second["strategy_id"],"scx-u600p0")

    def test_third_probe_would_step_geometrically_when_the_budget_allows(self):
        agent=WeightedAgent()
        first=weighted_proposal(agent,avg_wait_us=2400,background_runnable=4)
        agent.committed(first,25,"default")
        for seq in (28,31,34):
            window=dict(hdr_id="fresh",hdr_start=27,hdr_end=32,hdr_p99_ms=7,frontend_rps=1000,
                        completion_fraction=1,request_errors=0) if seq==34 else {}
            agent.observe(observation(seq,strategy=first["strategy_id"],probe_p95_ms=2,eps=700,**window))
        # Pretend the preemption probe was measured (and scored better), then ask again.
        agent.search["points"].append({"strategy":"scx-u600p0","slice_us":600,"preempt":False,"score":120.0})
        self.assertEqual(agent._next_probe(agent.last_observation),"scx-u500p0")

    def test_search_stops_after_the_budget_and_holds_the_best_point(self):
        agent=WeightedAgent()
        first=weighted_proposal(agent,avg_wait_us=2400,background_runnable=4)
        agent.committed(first,25,"default")
        for seq in (28,31,34):
            window=dict(hdr_id="fresh",hdr_start=27,hdr_end=32,hdr_p99_ms=7,frontend_rps=1000,
                        completion_fraction=1,request_errors=0) if seq==34 else {}
            agent.observe(observation(seq,strategy=first["strategy_id"],probe_p95_ms=2,eps=700,**window))
        second=None
        for seq in range(35,42):
            second=second or agent.observe(observation(seq))
        agent.committed(second,42,"default")
        action=None
        for seq in (45,48,51):
            window=dict(hdr_id="later",hdr_start=44,hdr_end=49,hdr_p99_ms=9,frontend_rps=1000,
                        completion_fraction=1,request_errors=0) if seq==51 else {}
            action=agent.observe(observation(seq,strategy=second["strategy_id"],probe_p95_ms=3,eps=600,**window)) or action
        self.assertEqual(action["kind"],"rollback")
        self.assertTrue(agent.search["done"])
        self.assertEqual(len(agent.search["points"]),2)
        agent.committed(action,52,second["strategy_id"])
        for seq in range(55,62):
            self.assertIsNone(agent.observe(observation(seq)))
        self.assertEqual(agent.phase,"holding")
        self.assertIn("探测点",agent.reason)

    def test_chosen_reports_the_parameter_the_agent_settled_on(self):
        agent=WeightedAgent()
        first=weighted_proposal(agent,avg_wait_us=2400,background_runnable=4)
        self.assertIsNone(agent.chosen())
        agent.committed(first,25,"default")
        for seq in (28,31,34):
            window=dict(hdr_id="fresh",hdr_start=27,hdr_end=32,hdr_p99_ms=7,frontend_rps=1000,
                        completion_fraction=1,request_errors=0) if seq==34 else {}
            agent.observe(observation(seq,strategy=first["strategy_id"],probe_p95_ms=2,eps=700,**window))
        chosen=agent.chosen()
        self.assertEqual(chosen["slice_us"],600)
        self.assertEqual(chosen["label"],"SCX 600μs + 唤醒抢占")
        self.assertEqual(chosen["probed_points"],1)
        self.assertGreater(chosen["weighted_score"],100)

    def test_parametric_ids_round_trip_and_stay_in_loader_bounds(self):
        self.assertEqual(scx_strategy_id(1000,True),"scx-1ms")
        self.assertEqual(scx_strategy_id(500,True),"scx-500us")
        self.assertEqual(scx_strategy_id(750,True),"scx-u750p1")
        self.assertEqual(strategy_spec("scx-u750p1")["slice_us"],750)
        self.assertEqual(strategy_spec("scx-u750p1")["label"],"SCX 750μs + 唤醒抢占")
        for bad in ("scx-u50p1","scx-u20001p1","scx-2ms","agent"):
            with self.assertRaises(ValueError):strategy_spec(bad)


if __name__=="__main__":unittest.main()
