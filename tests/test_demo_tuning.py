"""Selection and attribution regressions; samples here are synthetic fixtures."""
import unittest
from oeagent.demo_tuning import SCENARIOS, STRATEGIES, StrategySearch, aggregate, select


def sample(seq=1, start=10, **values):
    return {"window_id": str(seq), "start_monotonic": start, "end_monotonic": start + 5,
            "duration_seconds": 5, "source_sha256": "a" * 64, "p99_ms": 2,
            "rps": 25600, "completion_fraction": 1, "eps": 3000,
            "background_coverage": 1, "errors": 0, **values}


def rows(**overrides):
    trials = []
    for key in STRATEGIES:
        values = overrides.get(key, {})
        trials.append({"strategy_id": key, "status": "measured",
                       "windows": [sample(**values), sample(seq=2, **values)]})
    return aggregate(trials)


class TuningChecks(unittest.TestCase):
    def test_four_candidates_are_measured_without_scenario_winner(self):
        for scenario in SCENARIOS.values():
            search = StrategySearch(scenario, repeats=2)
            self.assertEqual({p["strategy_id"] for p in search.plan}, set(STRATEGIES))
            self.assertEqual(len(search.plan), 8)
            self.assertNotIn("winner", scenario)
        self.assertEqual(len(SCENARIOS), 8)

    def test_round_order_is_rotated(self):
        search = StrategySearch(SCENARIOS["latency"], repeats=2)
        self.assertNotEqual(search.plan[0]["strategy_id"], search.plan[4]["strategy_id"])

    def test_throughput_objective_can_choose_10ms(self):
        result = select(rows(**{"scx-10ms": {"eps": 3400}}), SCENARIOS["throughput"])
        self.assertEqual(result["strategy_id"], "scx-10ms")
        self.assertTrue(result["validated"])

    def test_latency_objective_can_choose_500us_or_1ms(self):
        for key in ("scx-500us", "scx-1ms"):
            result = select(rows(**{key: {"p99_ms": 1.5}}), SCENARIOS["latency"])
            self.assertEqual(result["strategy_id"], key)

    def test_fastest_latency_is_rejected_when_it_starves_batch(self):
        result = select(rows(**{"scx-500us": {"p99_ms": 1, "eps": 2000},
                                "scx-1ms": {"p99_ms": 1.5}}), SCENARIOS["latency"])
        self.assertEqual(result["strategy_id"], "scx-1ms")
        self.assertFalse(next(r for r in result["rows"] if r["strategy_id"] == "scx-500us")["eligible"])

    def test_default_is_retained_for_small_difference(self):
        result = select(rows(**{"scx-1ms": {"p99_ms": 1.99}}), SCENARIOS["latency"])
        self.assertEqual(result["strategy_id"], "default")
        result = select(rows(**{"scx-10ms": {"eps": 3001}}), SCENARIOS["batch"])
        self.assertEqual(result["strategy_id"], "default")

    def test_completion_loss_and_errors_cannot_win(self):
        result = select(rows(**{"scx-500us": {"p99_ms": 1, "completion_fraction": .8},
                                "scx-1ms": {"p99_ms": 1.2, "errors": 1}}), SCENARIOS["latency"])
        self.assertEqual(result["strategy_id"], "default")

    def test_all_slo_failures_are_explicit_safe_fallback(self):
        result = select(rows(**{key: {"p99_ms": 50} for key in STRATEGIES}), SCENARIOS["latency"])
        self.assertEqual(result["strategy_id"], "default")
        self.assertFalse(result["validated"])

    def test_pure_batch_has_no_fabricated_http_metrics(self):
        measured = rows(**{key: {"p99_ms": None, "rps": None, "completion_fraction": None} for key in STRATEGIES})
        result = select(measured, SCENARIOS["batch"])
        self.assertTrue(result["validated"])
        self.assertIsNone(result["reference"]["p99_ms"])

    def test_cross_policy_stale_duplicate_and_low_coverage_windows_are_rejected(self):
        search = StrategySearch(SCENARIOS["latency"], windows=2)
        context = {"revision": 1}
        search.begin(0, context)
        for item, now, ctx in ((sample(start=1), 6, context),
                              (sample(), 30, context), (sample(), 15, {"revision": 2}),
                              (sample(background_coverage=.5), 15, context)):
            self.assertFalse(search.accept(item, now, ctx))
        self.assertFalse(search.accept(sample(), 15, context))
        self.assertFalse(search.accept(sample(), 15, context))
        self.assertEqual(len(search.current["windows"]), 1)
        self.assertTrue(search.accept(sample(seq=2, start=15), 20, context))
        self.assertEqual(search.stage, "pending")

    def test_incomplete_repeated_periods_cannot_be_selected(self):
        result = select(rows(), SCENARIOS["latency"], expected_periods=2)
        self.assertFalse(result["validated"])

    def test_more_than_one_round_yields_sd_over_period_medians(self):
        trials = [{"strategy_id": "default", "status": "measured", "windows": [sample(p99_ms=value)]}
                  for value in (1, 3)]
        result = aggregate(trials)[0]
        self.assertEqual(result["p99_ms"], 2)
        self.assertAlmostEqual(result["p99_ms_sd"], 2 ** .5)

    def test_parameter_validation_prevents_empty_or_unbounded_search(self):
        for values in ({"windows": 0}, {"windows": 100}, {"repeats": True}, {"repeats": 100}):
            with self.assertRaises(ValueError):
                StrategySearch(SCENARIOS["latency"], **values)

    def test_different_explicit_business_goals_change_eligibility(self):
        measurements = rows(**{"scx-1ms": {"p99_ms": 1, "eps": 2500}})
        strict = select(measurements, SCENARIOS["latency"])
        relaxed = select(measurements, {**SCENARIOS["latency"], "eps_floor_fraction": .8})
        self.assertEqual(strict["strategy_id"], "default")
        self.assertEqual(relaxed["strategy_id"], "scx-1ms")


if __name__ == "__main__":
    unittest.main()
