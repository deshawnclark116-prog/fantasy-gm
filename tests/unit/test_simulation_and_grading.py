from __future__ import annotations

import numpy as np
import pytest

from fantasy_gm.domain.decision import OutcomeDistribution
from fantasy_gm.domain.seeds import SeedSpec
from fantasy_gm.grading.grader import pit_from_quantiles
from fantasy_gm.simulation.rng import generator_for


def test_seed_children_are_stable_and_distinct() -> None:
    root = SeedSpec(root_seed=1)
    a1 = generator_for(root.child("a")).random(5)
    a2 = generator_for(SeedSpec(root_seed=1, path=("a",))).random(5)
    b = generator_for(root.child("b")).random(5)
    np.testing.assert_array_equal(a1, a2)
    assert not np.array_equal(a1, b)


def test_seed_spec_bounds() -> None:
    with pytest.raises(ValueError, match="greater than or equal"):
        SeedSpec(root_seed=-1)


def test_pit_interpolation_and_range_flags() -> None:
    dist = OutcomeDistribution(
        metric="m", unit="u", mean=10, quantiles={0.1: 0, 0.5: 10, 0.9: 20}, method="t"
    )
    assert pit_from_quantiles(dist, 10) == (0.5, False)
    assert pit_from_quantiles(dist, 15)[0] == pytest.approx(0.7)
    assert pit_from_quantiles(dist, -5) == (0.1, True)
    assert pit_from_quantiles(dist, 50) == (0.9, True)
    single = dist.model_copy(update={"quantiles": {0.5: 10.0}})
    assert pit_from_quantiles(single, 10) == (None, False)
