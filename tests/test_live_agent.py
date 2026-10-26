"""Scene-free decision, confirmation, feedback and rollback contracts."""
from dataclasses import fields, replace
import unittest

from oeagent.live_agent import AgentGoals, LiveAgent, Observation


def observation(sequence=1, **changes):
    values = dict(sequence=sequence, now=float(sequence), identity="same-live-processes", strategy="default",
        frontend_present=True, background_present=True, cpu_busy_pct=95, frontend_cpu_pct=20,
        background_cpu_pct=75, wait_pct=75, avg_wait_us=1500, background_runnable=1,
        voluntary_per_sec=10, involuntary_per_sec=300, probe_p95_ms=8,
        probe_samples=5, probe_errors=0, eps=1000)
    values.update(changes)
    return Observation(**values)


def propose(agent, **changes):
    action = None
    for sequence in range(1, 7):
        action = agent.observe(observation(sequence, **changes))
        if action:
            return action
    return action


class LiveAgentChecks(unittest.TestCase):
    def test_input_contract_has_no_scene_name_or_saved_winner(self):
        names = {field.name for field in fields(Observation)}
        self.assertFalse(names.intersection({"scenario", "scenario_id", "label", "history", "winner", "batch_kind", "objective"}))
        with self.assertRaises(TypeError):
            observation(scenario_id="latency")

    def test_same_identity_different_live_pressure_changes_decision(self):
        pressured = propose(LiveAgent())
        self.assertEqual(pressured["strategy_id"], "scx-1ms")
        self.assertEqual(pressured["kind"], "trial")
        quiet = LiveAgent()
        self.assertIsNone(propose(quiet, cpu_busy_pct=25, wait_pct=1, avg_wait_us=1,
                                  background_cpu_pct=0, background_present=False, probe_p95_ms=1))
        self.assertEqual(quiet.identity, "same-live-processes")

    def test_single_spike_does_not_apply_candidate(self):
        agent = LiveAgent()
        self.assertIsNone(agent.observe(observation()))
        self.assertIsNone(agent.observe(observation(2, cpu_busy_pct=20, wait_pct=0)))
        self.assertIsNone(agent.observe(observation(3, cpu_busy_pct=20, wait_pct=0)))

    def test_residual_latency_on_1ms_can_propose_500us(self):
        action = propose(LiveAgent(), strategy="scx-1ms", wait_pct=10, avg_wait_us=100)
        self.assertEqual(action["strategy_id"], "scx-500us")

    def test_measured_multiple_cpu_workers_can_propose_10ms(self):
        action = propose(LiveAgent(), frontend_present=False, frontend_cpu_pct=0,
            background_cpu_pct=95, background_runnable=4, wait_pct=None, avg_wait_us=None,
            probe_p95_ms=None, probe_samples=0)
        self.assertEqual(action["strategy_id"], "scx-10ms")
        self.assertEqual(action["trial_kind"], "throughput")

    def test_low_measured_pressure_recovers_default(self):
        action = propose(LiveAgent(), strategy="scx-1ms", cpu_busy_pct=25,
            wait_pct=1, avg_wait_us=1, background_cpu_pct=0, background_present=False, probe_p95_ms=1)
        self.assertEqual(action["strategy_id"], "default")
        self.assertEqual(action["kind"], "recovery")

    def candidate(self):
        agent = LiveAgent()
        action = propose(agent)
        agent.committed(action, 5, "default")
        return agent

    def test_candidate_is_kept_only_after_new_feedback(self):
        agent = self.candidate()
        for sequence in (8, 11, 14):
            window = dict(hdr_id="candidate-window", hdr_start=7, hdr_end=12,
                          hdr_p99_ms=2, frontend_rps=1000, completion_fraction=1, request_errors=0) if sequence == 14 else {}
            result = agent.observe(observation(sequence, strategy="scx-1ms", wait_pct=5, avg_wait_us=20,
                probe_p95_ms=1, eps=950, **window))
            self.assertIsNone(result)
        self.assertEqual(agent.phase, "accepted")
        self.assertEqual(agent.events[-1]["action"], "accept")

    def test_throughput_loss_rejects_candidate_and_prevents_immediate_retry(self):
        agent = self.candidate()
        action = None
        for sequence in (8, 11, 14):
            action = agent.observe(observation(sequence, strategy="scx-1ms", probe_p95_ms=1, wait_pct=5, eps=500))
        self.assertEqual(action["strategy_id"], "default")
        self.assertEqual(action["kind"], "rollback")
        agent.committed(action, 14, "scx-1ms")
        for sequence in range(15, 25):
            self.assertIsNone(agent.observe(observation(sequence)))
        self.assertEqual(agent.phase, "cooldown")

    def test_fast_latency_gain_cannot_hide_hdr_violation(self):
        agent = self.candidate()
        action = agent.observe(observation(13, strategy="scx-1ms", probe_p95_ms=.1,
            hdr_id="bad", hdr_start=7, hdr_end=12, hdr_p99_ms=30, frontend_rps=1000,
            completion_fraction=1, request_errors=0))
        self.assertEqual(action["kind"], "rollback")

    def test_probe_failures_recover_safely(self):
        agent = LiveAgent()
        self.assertIsNone(agent.observe(observation(1, strategy="scx-10ms", probe_errors=1, probe_samples=0)))
        action = agent.observe(observation(2, strategy="scx-10ms", probe_errors=1, probe_samples=0))
        self.assertEqual(action["strategy_id"], "default")

    def test_process_identity_change_invalidates_comparison_without_selecting_config(self):
        agent = self.candidate()
        result = agent.observe(observation(8, identity="new-processes", strategy="scx-1ms"))
        self.assertIsNone(result)
        self.assertIsNone(agent.trial)
        self.assertEqual(agent.phase, "observing")

    def test_duplicate_input_is_not_another_confirmation(self):
        agent = LiveAgent()
        item = observation()
        for _ in range(20):
            self.assertIsNone(agent.observe(item))
        self.assertEqual(len(agent.samples), 1)

    def test_global_goals_are_validated_without_any_scene(self):
        with self.assertRaises(ValueError):
            AgentGoals(latency_ms=float("nan"))
        agent = self.candidate()
        agent.configure(AgentGoals(latency_ms=8, background_floor=.9))
        self.assertEqual(agent.goals.latency_ms, 8)
        self.assertIsNone(agent.trial)

    def test_trial_without_full_business_window_times_out(self):
        agent = self.candidate()
        action = None
        for sequence in (8, 11, 14, 30):
            action = agent.observe(observation(sequence, strategy="scx-1ms", probe_p95_ms=1, wait_pct=5, eps=950))
        self.assertEqual(action["kind"], "rollback")
        self.assertIn("完整业务窗口", action["reason"])

    def test_repeated_hdr_window_does_not_count_as_two_failures(self):
        agent = LiveAgent()
        window = dict(hdr_id="one", hdr_start=1, hdr_end=6, hdr_p99_ms=10,
                      frontend_rps=1000, completion_fraction=1, request_errors=0)
        for sequence in (6, 7, 8):
            self.assertIsNone(agent.observe(observation(sequence, strategy="scx-500us", **window)))
        self.assertEqual(agent.monitor_bad_windows, 1)
        window.update(hdr_id="two", hdr_start=6, hdr_end=11)
        action = agent.observe(observation(11, strategy="scx-500us", **window))
        self.assertEqual(action["strategy_id"], "default")

    def test_business_windows_require_real_complete_past_data(self):
        for changes in (dict(hdr_end=20), dict(hdr_start=8, hdr_end=9), dict(hdr_p99_ms=None)):
            window = dict(hdr_id="invalid", hdr_start=1, hdr_end=6, hdr_p99_ms=1,
                          frontend_rps=1000, completion_fraction=1, request_errors=0)
            window.update(changes)
            with self.assertRaises(ValueError):
                observation(10, **window)

    def test_incomplete_data_cannot_choose_a_candidate(self):
        agent = LiveAgent()
        self.assertIsNone(propose(agent, valid=False, cpu_busy_pct=None))
        self.assertEqual(agent.phase, "observing")
        with self.assertRaises(ValueError):
            observation(eps=float("inf"))


if __name__ == "__main__":
    unittest.main()
