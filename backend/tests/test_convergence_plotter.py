"""Parser / plotter regression tests against a real implicit messag file."""

from convergence_plotter import (
    ConvergenceParser,
    compute_optimal_timestep,
    parse_and_plot_json,
)
from status_page import parse_step_size


def test_parser_summary(messag_content):
    parser = ConvergenceParser(messag_content)
    parser.parse()
    summary = parser.get_summary()

    assert summary["total_steps"] == 28
    assert summary["total_iterations"] > 0
    assert summary["converged_steps"] > 0
    assert summary["converged_steps"] + summary["failed_steps"] <= summary["total_steps"]
    assert summary["current_sim_time"] is not None
    assert summary["current_step_size"] is not None
    assert summary["current_step_size"] > 0


def test_parser_handles_truncated_file(messag_content):
    """The parser must cope with a live file cut off mid-step."""
    truncated = messag_content[: len(messag_content) // 2]
    parser = ConvergenceParser(truncated)
    parser.parse()
    summary = parser.get_summary()
    assert 0 < summary["total_steps"] <= 28


def test_parse_and_plot_json_keys(messag_content):
    summary, plots = parse_and_plot_json(messag_content)

    expected = {
        "iterations_per_timestep",
        "time_duration",
        "displacement_norm",
        "energy_norm",
        "convergence_status",
        "displacement_evolution",
    }
    assert expected.issubset(plots.keys())
    # Every figure must be a JSON-serialisable dict with data + layout
    for key in expected:
        fig = plots[key]
        assert isinstance(fig, dict)
        assert "data" in fig and "layout" in fig


def test_compute_optimal_timestep(messag_content):
    points = compute_optimal_timestep(messag_content)
    assert len(points) > 0
    times = [t for t, _ in points]
    dts = [dt for _, dt in points]
    assert times == sorted(times)
    assert all(dt > 0 for dt in dts)
    # Change-points only: consecutive dt values must differ
    assert all(a != b for a, b in zip(dts, dts[1:]))


def test_parse_step_size(messag_content):
    value = parse_step_size(messag_content)
    assert value != "—"
    assert float(value) > 0


def test_parse_step_size_no_marker():
    assert parse_step_size("no implicit content here\nat all\n") == "—"
