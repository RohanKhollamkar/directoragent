"""Routing tables and cost math (STEP 11)."""

from directoragent.routing import (
    COST_PER_SECOND,
    drift_threshold,
    estimate_cost,
    route,
)
from directoragent.schema import Model, RenderClass


def test_route_maps_each_render_class_to_model():
    expected = {
        RenderClass.FACE: Model.SEEDANCE_2,
        RenderClass.COMPLEX_MOTION: Model.KLING_3,
        RenderClass.ABSTRACT_FLUID: Model.WAN_2_6,
        RenderClass.WIDE_ENVIRONMENT: Model.VEO_3_1,
    }
    for rc, model in expected.items():
        got_model, reason = route(rc)
        assert got_model is model
        assert isinstance(reason, str) and reason


def test_drift_threshold_values():
    assert drift_threshold(RenderClass.FACE) == 0.78
    assert drift_threshold(RenderClass.COMPLEX_MOTION) == 0.72
    assert drift_threshold(RenderClass.ABSTRACT_FLUID) == 0.65
    assert drift_threshold(RenderClass.WIDE_ENVIRONMENT) == 0.70


def test_estimate_cost_math():
    # estimate_cost = COST_PER_SECOND[model] * duration
    assert estimate_cost(Model.SEEDANCE_2, 10) == COST_PER_SECOND[Model.SEEDANCE_2] * 10
    assert estimate_cost(Model.VEO_3_1, 18) == COST_PER_SECOND[Model.VEO_3_1] * 18
    assert estimate_cost(Model.SEEDANCE_2, 0) == 0.0
    assert abs(estimate_cost(Model.KLING_3, 22) - 0.14 * 22) < 1e-9
