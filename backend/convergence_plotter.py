"""
Convergence Plotter - Parse messag files and generate interactive Plotly visualizations

Extracts convergence norms from LS-DYNA simulation output files and creates
interactive Plotly charts for display in the dashboard.
"""

import re
import json
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime
import plotly.graph_objects as go
import numpy as np


class ConvergenceParser:
    """
    Parser for LS-DYNA messag files

    Extracts simulation data including:
    - Time step information
    - Iteration data with convergence metrics
    - Termination status
    """

    def __init__(self, content: str):
        """
        Initialize parser with messag file content

        Args:
            content: Messag file content as string
        """
        self.content = content
        self.lines = content.splitlines()
        self.data = {
            "simulationStart": {"date": None, "time": None},
            "hostname": None,
            "timeSteps": [],
            "terminationStatus": "in-progress",
            "totalIterations": 0,
            "convergedSteps": 0,
            "failedSteps": 0,
            "errorAnalysis": None,
        }

    def parse(self) -> Dict[str, Any]:
        """Main parsing function"""
        self._extract_start_info()
        self._extract_hostname()
        self._extract_timesteps_and_iterations()
        self._extract_termination_status()
        self._calculate_statistics()

        return self.data

    def _extract_start_info(self):
        """Extract simulation start date and time"""
        for line in self.lines[:200]:
            match = re.search(r"Date:\s+(\d+/\d+/\d+)\s+Time:\s+(\d+:\d+:\d+)", line)
            if match:
                self.data["simulationStart"]["date"] = match.group(1)
                self.data["simulationStart"]["time"] = match.group(2)
                break

    def _extract_hostname(self):
        """Extract hostname from the header"""
        for line in self.lines[:200]:
            match = re.search(r"\|\s+Hostname\s+:\s+(\S+)", line)
            if match:
                self.data["hostname"] = match.group(1)
                break

    def _extract_timesteps_and_iterations(self):
        """Extract all time steps and their iterations"""
        i = 0
        step_number = 0

        while i < len(self.lines):
            line = self.lines[i]

            if re.search(r"BEGIN implicit", line):
                step_number += 1
                step_data = self._parse_timestep(self.lines, i, step_number)
                if step_data:
                    self.data["timeSteps"].append(step_data)
                    i += 50
                    continue

            i += 1

    def _parse_timestep(
        self, lines: List[str], start_idx: int, step_num: int
    ) -> Optional[Dict]:
        """Parse a single time step and all its iterations"""
        step_data = {
            "stepNumber": step_num,
            "startTime": {"date": None, "time": None},
            "convergenceStatus": "in-progress",
            "iterations": [],
            "targetTime": None,
            "stepSize": None,
        }

        for offset in range(5):
            if start_idx + offset < len(lines):
                match = re.search(
                    r"(\d+/\d+/\d+)\s+(\d+:\d+:\d+)", lines[start_idx + offset]
                )
                if match:
                    step_data["startTime"]["date"] = match.group(1)
                    step_data["startTime"]["time"] = match.group(2)
                    break

        for offset in range(10):
            if start_idx + offset < len(lines):
                line = lines[start_idx + offset]
                if "time =" in line:
                    match = re.search(r"time\s+=\s+([\d.E+-]+)", line)
                    if match:
                        step_data["targetTime"] = match.group(1)
                if "current step size" in line:
                    match = re.search(r"current step size\s+=\s+([\d.E+-]+)", line)
                    if match:
                        step_data["stepSize"] = match.group(1)

        i = start_idx
        max_search = min(start_idx + 10000, len(lines))

        while i < max_search:
            line = lines[i]

            if i > start_idx + 10:
                if re.search(r"REFORMATION LIMIT reached.*aborted", line):
                    step_data["convergenceStatus"] = "failed"
                    break
                if re.search(r"failed step", line, re.IGNORECASE):
                    step_data["convergenceStatus"] = "failed"
                    break
                if re.search(r"Convergence detected", line):
                    step_data["convergenceStatus"] = "converged"
                    break
                if re.search(r"termination", line, re.IGNORECASE):
                    if step_data["convergenceStatus"] == "in-progress":
                        if len(step_data["iterations"]) > 0:
                            step_data["convergenceStatus"] = "failed"
                    break
                if re.search(r"BEGIN implicit", line):
                    break

            if "Iteration:" in line:
                iter_data = self._parse_iteration(lines, i)
                if iter_data:
                    step_data["iterations"].append(iter_data)

            i += 1

        return step_data

    def _parse_iteration(self, lines: List[str], idx: int) -> Optional[Dict]:
        """Parse a single iteration's data"""
        line = lines[idx]

        match = re.search(r"Iteration:\s+(\d+)", line)
        if not match:
            return None

        iter_num = int(match.group(1))

        iter_date = None
        iter_time = None

        for offset in range(1, min(11, idx + 1)):
            prev_line = lines[idx - offset]
            date_match = re.search(r"(\d+/\d+/\d+)\s+(\d+:\d+:\d+)", prev_line)
            if date_match:
                iter_date = date_match.group(1)
                iter_time = date_match.group(2)
                break

        displacement = "n/a"
        energy = "n/a"
        residual = "n/a"

        if idx + 2 < len(lines):
            norm_line = lines[idx + 2]
            if "norm ratio" in norm_line:
                values = re.findall(r"([\d.E+-]+|n/a)", norm_line)
                if len(values) >= 3:
                    displacement = values[0] if values[0] != "n/a" else "n/a"
                    energy = values[1] if values[1] != "n/a" else "n/a"
                    residual = values[2] if values[2] != "n/a" else "n/a"

        return {
            "iterationNumber": iter_num,
            "startTime": {"date": iter_date, "time": iter_time},
            "displacement": displacement,
            "energy": energy,
            "residual": residual,
        }

    def _extract_termination_status(self):
        """Check for termination markers"""
        for line in self.lines[-200:]:
            if re.search(r"E r r o r\s+t e r m i n a t i o n", line):
                self.data["terminationStatus"] = "error"
                return
            if re.search(r"N o r m a l\s+t e r m i n a t i o n", line):
                self.data["terminationStatus"] = "normal"
                return

    def _calculate_statistics(self):
        """Calculate summary statistics"""
        total_iters = 0
        converged = 0
        failed = 0

        for step in self.data["timeSteps"]:
            total_iters += len(step["iterations"])
            if step["convergenceStatus"] == "converged":
                converged += 1
            elif step["convergenceStatus"] == "failed":
                failed += 1

        self.data["totalIterations"] = total_iters
        self.data["convergedSteps"] = converged
        self.data["failedSteps"] = failed

    def get_summary(self) -> Dict[str, Any]:
        """Get summary statistics"""
        last_step = self.data["timeSteps"][-1] if self.data["timeSteps"] else None
        try:
            current_sim_time = float(last_step.get("targetTime")) if last_step else None
        except (TypeError, ValueError):
            current_sim_time = None
        return {
            "hostname": self.data["hostname"],
            "start_date": self.data["simulationStart"]["date"],
            "start_time": self.data["simulationStart"]["time"],
            "total_steps": len(self.data["timeSteps"]),
            "total_iterations": self.data["totalIterations"],
            "converged_steps": self.data["convergedSteps"],
            "failed_steps": self.data["failedSteps"],
            "termination_status": self.data["terminationStatus"],
            "current_sim_time": current_sim_time,
        }


class ConvergencePlotter:
    """
    Generates interactive Plotly visualizations from parsed messag data

    Creates publication-quality interactive charts including:
    - Iterations per timestep with convergence status
    - Time duration analysis
    - Convergence metrics evolution (displacement, energy, residual)
    - Convergence status summary (pie chart)
    - Displacement evolution across all timesteps
    """

    # Colors inspired by IPA theme (blue-teal gradient) with semantic meaning
    COLOR_CONVERGED = "#2ecc71"    # Green for success
    COLOR_FAILED = "#e74c3c"       # Red for failure
    COLOR_IN_PROGRESS = "#f39c12"  # Orange for in-progress
    COLOR_AVERAGE = "#0a4d6e"      # Theme dark blue
    COLOR_THRESHOLD = "#e74c3c"    # Red for threshold
    COLOR_DATA = "#2d9ea8"         # Theme teal for data

    def __init__(self, data: Dict[str, Any]):
        """
        Initialize the plotter

        Args:
            data: Parsed simulation data dictionary
        """
        self.data = data

    def _safe_float(self, value: str) -> Optional[float]:
        """Safely convert string to float"""
        try:
            return float(value)
        except (ValueError, TypeError):
            return None

    def plot_iterations_per_timestep(self) -> go.Figure:
        """
        Generate bar chart of iterations per timestep with convergence status

        Returns:
            Plotly figure
        """
        fig = go.Figure()

        steps = []
        iterations = []
        colors = []
        sim_times = []
        step_sizes = []

        prev_time = None
        for step in self.data["timeSteps"]:
            steps.append(step["stepNumber"])
            iterations.append(len(step["iterations"]))

            t = self._safe_float(step.get("targetTime"))
            sim_times.append(t if t is not None else 0.0)
            step_sizes.append((t - prev_time) if (t is not None and prev_time is not None) else 0.0)
            prev_time = t

            status = step["convergenceStatus"]
            if status == "converged":
                colors.append(self.COLOR_CONVERGED)
            elif status == "failed":
                colors.append(self.COLOR_FAILED)
            else:
                colors.append(self.COLOR_IN_PROGRESS)

        fig.add_trace(
            go.Bar(
                x=steps,
                y=iterations,
                marker_color=colors,
                marker_line_color="black",
                marker_line_width=0.5,
                text=iterations,
                textposition="outside",
                customdata=list(zip(sim_times, step_sizes)),
                hovertemplate=(
                    "Step: %{x}<br>"
                    "Iterations: %{y}<br>"
                    "Sim Time: %{customdata[0]:.4g}<br>"
                    "Step Size (dt): %{customdata[1]:.4g}"
                    "<extra></extra>"
                ),
            )
        )

        if iterations:
            avg_iters = np.mean(iterations)
            fig.add_hline(
                y=avg_iters,
                line_dash="dash",
                line_color=self.COLOR_AVERAGE,
                annotation_text=f"Average: {avg_iters:.2f}",
                annotation_position="top right",
            )

        fig.update_layout(
            xaxis_title="Time Step Number",
            yaxis_title="Number of Iterations",
            title="Iterations per Time Step<br><sup>Green=Converged, Red=Failed, Yellow=In-Progress</sup>",
            showlegend=False,
            height=400,
            margin=dict(l=50, r=50, t=80, b=50),
        )

        return fig

    def plot_time_duration_per_timestep(self) -> go.Figure:
        """
        Generate bar chart of time duration per timestep

        Returns:
            Plotly figure
        """
        fig = go.Figure()

        steps = []
        durations = []
        colors = []
        sim_times_dur = []
        step_sizes_dur = []

        for i, step in enumerate(self.data["timeSteps"]):
            if i == 0:
                continue

            prev_step = self.data["timeSteps"][i - 1]

            if (
                step["iterations"]
                and prev_step["iterations"]
                and step["iterations"][0]["startTime"]["time"]
                and prev_step["iterations"][-1]["startTime"]["time"]
            ):
                try:
                    curr_time_str = (
                        f"{step['startTime']['date']} {step['startTime']['time']}"
                    )
                    prev_time_str = f"{prev_step['startTime']['date']} {prev_step['startTime']['time']}"

                    curr_time = datetime.strptime(curr_time_str, "%m/%d/%y %H:%M:%S")
                    prev_time = datetime.strptime(prev_time_str, "%m/%d/%y %H:%M:%S")

                    duration_sec = (curr_time - prev_time).total_seconds()

                    steps.append(step["stepNumber"])
                    durations.append(duration_sec)
                    t_cur = self._safe_float(step.get("targetTime"))
                    t_prev = self._safe_float(prev_step.get("targetTime"))
                    sim_times_dur.append(t_cur if t_cur is not None else 0.0)
                    _dt = (t_cur - t_prev) if (t_cur is not None and t_prev is not None) else 0.0
                    step_sizes_dur.append(_dt)

                    status = step["convergenceStatus"]
                    if status == "converged":
                        colors.append(self.COLOR_CONVERGED)
                    elif status == "failed":
                        colors.append(self.COLOR_FAILED)
                    else:
                        colors.append(self.COLOR_IN_PROGRESS)
                except Exception:
                    pass

        if durations:
            fig.add_trace(
                go.Bar(
                    x=steps,
                    y=durations,
                    marker_color=colors,
                    marker_line_color="black",
                    marker_line_width=0.5,
                    customdata=list(zip(sim_times_dur, step_sizes_dur)),
                    hovertemplate=(
                        "Step: %{x}<br>"
                        "Duration: %{y:.1f}s<br>"
                        "Sim Time: %{customdata[0]:.4g}<br>"
                        "Step Size (dt): %{customdata[1]:.4g}"
                        "<extra></extra>"
                    ),
                )
            )

            avg_duration = np.mean(durations)
            fig.add_hline(
                y=avg_duration,
                line_dash="dash",
                line_color=self.COLOR_AVERAGE,
                annotation_text=f"Average: {avg_duration:.2f}s",
                annotation_position="top right",
            )
        else:
            fig.update_layout(
                annotations=[
                    dict(
                        text="Insufficient timestamp data for duration calculation",
                        x=0.5,
                        y=0.5,
                        showarrow=False,
                        font=dict(size=14),
                    )
                ]
            )

        fig.update_layout(
            xaxis_title="Time Step Number",
            yaxis_title="Duration (seconds)",
            title="Time Duration per Time Step",
            height=400,
            margin=dict(l=50, r=50, t=80, b=50),
        )

        return fig

    def _build_norm_figure(
        self,
        metric: str,
        title: str,
        yaxis_title: str,
    ) -> go.Figure:
        """
        Shared helper — builds a standalone figure for one norm metric
        (displacement or energy) across the last 10 timesteps.

        Args:
            metric:      'displacement' or 'energy'
            title:       Chart title string
            yaxis_title: Y-axis label string

        Returns:
            Plotly figure
        """
        fig = go.Figure()

        num_steps_to_plot = min(10, len(self.data["timeSteps"]))
        steps_to_plot = self.data["timeSteps"][-num_steps_to_plot:]

        # Precompute (targetTime, stepSize) for every step in the full list
        all_steps = self.data["timeSteps"]
        step_time_info: dict = {}  # stepNumber → (targetTime, dt)
        for i, s in enumerate(all_steps):
            t = self._safe_float(s.get("targetTime"))
            t_prev = self._safe_float(all_steps[i - 1].get("targetTime")) if i > 0 else None
            dt = (t - t_prev) if (t is not None and t_prev is not None) else None
            step_time_info[s["stepNumber"]] = (t, dt)

        for step in steps_to_plot:
            if not step["iterations"]:
                continue

            points = []
            for iteration in step["iterations"]:
                raw = self._safe_float(iteration[metric])
                if raw is not None:
                    val = abs(raw) if metric == "energy" else raw
                    points.append((iteration["iterationNumber"], val))

            if not points:
                continue

            x, y = zip(*points)
            t_val, dt_val = step_time_info.get(step["stepNumber"], (None, None))

            # Build compact legend label including sim time
            t_str = f"  t={t_val:.3g}" if t_val is not None else ""
            label = f"Step {step['stepNumber']}{t_str}"
            alpha = 0.7 if step["convergenceStatus"] == "converged" else 0.4

            # Per-point customdata: [sim_time, step_size] — same value repeated
            n_pts = len(x)
            cd = [[t_val if t_val is not None else 0.0,
                   dt_val if dt_val is not None else 0.0]] * n_pts

            fig.add_trace(
                go.Scatter(
                    x=x,
                    y=y,
                    mode="lines+markers",
                    name=label,
                    line=dict(width=2),
                    marker=dict(size=6),
                    opacity=alpha,
                    customdata=cd,
                    hovertemplate=(
                        f"Step {step['stepNumber']}<br>"
                        "Iter: %{x}<br>"
                        "Norm: %{y:.4g}<br>"
                        "Sim Time: %{customdata[0]:.4g}<br>"
                        "Step Size (dt): %{customdata[1]:.4g}"
                        "<extra></extra>"
                    ),
                )
            )

        fig.update_layout(
            title=title,
            xaxis_title="Iteration Number",
            yaxis_title=yaxis_title,
            yaxis_type="log",
            height=420,
            margin=dict(l=60, r=160, t=70, b=50),
            showlegend=True,
            legend=dict(
                orientation="v",
                x=1.02,
                y=1,
                xanchor="left",
                yanchor="top",
            ),
        )

        return fig

    def plot_displacement_norm_metrics(self) -> go.Figure:
        """Displacement norm ratio vs iteration number, last 10 timesteps."""
        return self._build_norm_figure(
            metric="displacement",
            title="Displacement Norm Ratio — Last 10 Time Steps",
            yaxis_title="Displacement Norm Ratio",
        )

    def plot_energy_norm_metrics(self) -> go.Figure:
        """Energy norm ratio (abs) vs iteration number, last 10 timesteps."""
        return self._build_norm_figure(
            metric="energy",
            title="Energy Norm Ratio (abs) — Last 10 Time Steps",
            yaxis_title="Energy Norm Ratio (abs)",
        )

    def plot_convergence_status(self) -> go.Figure:
        """
        Generate pie chart showing convergence status distribution

        Returns:
            Plotly figure
        """
        converged = self.data["convergedSteps"]
        failed = self.data["failedSteps"]
        in_progress = len(self.data["timeSteps"]) - converged - failed

        labels = ["Converged", "Failed", "In-Progress"]
        sizes = [converged, failed, in_progress]
        colors = [self.COLOR_CONVERGED, self.COLOR_FAILED, self.COLOR_IN_PROGRESS]

        filtered_data = [(l, s, c) for l, s, c in zip(labels, sizes, colors) if s > 0]
        if filtered_data:
            labels, sizes, colors = zip(*filtered_data)

        fig = go.Figure(
            data=[
                go.Pie(
                    labels=labels,
                    values=sizes,
                    marker=dict(colors=colors),
                    textinfo="label+percent",
                    hole=0.4,
                    hovertemplate="%{label}<br>%{value} steps<br>%{percent}<extra></extra>",
                )
            ]
        )

        total_steps = len(self.data["timeSteps"])
        fig.update_layout(
            title=f"Time Step Convergence Status<br><sup>Total: {total_steps} steps</sup>",
            height=400,
            margin=dict(l=50, r=50, t=80, b=50),
            annotations=[
                dict(
                    text=f"{total_steps}<br>steps",
                    x=0.5,
                    y=0.5,
                    font_size=14,
                    showarrow=False,
                )
            ],
        )

        return fig

    def plot_displacement_evolution(self) -> go.Figure:
        """
        Generate plot of displacement evolution across all timesteps.

        Each data point is colour-coded by time-step number using a Jet
        (blue → red) rainbow scale so it is immediately apparent whether
        spikes occurred at the start, middle, or end of the simulation.

        X-axis: simulation time
        Y-axis: displacement norm ratio (log scale)
        Colour:  step number — blue = early steps, red = late steps

        Returns:
            Plotly figure
        """
        fig = go.Figure()

        all_displacements = []
        all_times = []
        all_iter_nums = []   # iteration number within its timestep (resets to 1 each step)
        all_step_nums = []   # kept for hover tooltip only

        max_iter = 1  # track max iterations seen across all steps for colorscale

        for step in self.data["timeSteps"]:
            target_time = self._safe_float(step.get("targetTime"))
            if target_time is None:
                continue

            step_num = step["stepNumber"]
            for iteration in step["iterations"]:
                disp = self._safe_float(iteration["displacement"])
                if disp is not None and disp > 0:
                    iter_num = iteration["iterationNumber"]
                    all_displacements.append(disp)
                    all_times.append(target_time)
                    all_iter_nums.append(iter_num)
                    all_step_nums.append(step_num)
                    if iter_num > max_iter:
                        max_iter = iter_num

        if all_displacements and all_times:
            # Thin neutral line connecting points in order (drawn behind markers)
            fig.add_trace(
                go.Scatter(
                    x=all_times,
                    y=all_displacements,
                    mode="lines",
                    line=dict(color="rgba(100,130,160,0.25)", width=1),
                    showlegend=False,
                    hoverinfo="skip",
                )
            )

            # Markers colour-coded by iteration number within each timestep.
            # Jet: iteration 1 → blue, last iteration → red.
            # The colour resets to blue at the start of every new timestep,
            # making it easy to spot which iterations within a step had spikes.
            fig.add_trace(
                go.Scatter(
                    x=all_times,
                    y=all_displacements,
                    mode="markers",
                    marker=dict(
                        size=6,
                        color=all_iter_nums,
                        colorscale="Jet",
                        cmin=1,
                        cmax=max_iter,
                        colorbar=dict(
                            title=dict(text="Iteration #", side="right"),
                            thickness=14,
                            len=0.85,
                        ),
                        showscale=True,
                        opacity=0.85,
                    ),
                    customdata=list(zip(all_step_nums, all_iter_nums)),
                    hovertemplate=(
                        "Time: %{x:.4g}<br>"
                        "Disp Norm: %{y:.4g}<br>"
                        "Step: %{customdata[0]}, Iter: %{customdata[1]}"
                        "<extra></extra>"
                    ),
                    showlegend=False,
                )
            )

            fig.add_hline(
                y=0.02,
                line_dash="dash",
                line_color=self.COLOR_THRESHOLD,
                annotation_text="Typical Convergence Threshold (0.02)",
                annotation_position="top right",
            )

        fig.update_layout(
            xaxis_title="Simulation Time",
            yaxis_title="Displacement Norm Ratio (log scale)",
            title="Displacement Evolution Across All Time Steps",
            height=450,
            margin=dict(l=50, r=80, t=80, b=50),
            yaxis_type="log",
        )

        return fig

    def get_all_plots(self) -> Dict[str, go.Figure]:
        """Generate all plots and return as dictionary"""
        return {
            "iterations_per_timestep": self.plot_iterations_per_timestep(),
            "time_duration": self.plot_time_duration_per_timestep(),
            "displacement_norm": self.plot_displacement_norm_metrics(),
            "energy_norm": self.plot_energy_norm_metrics(),
            "convergence_status": self.plot_convergence_status(),
            "displacement_evolution": self.plot_displacement_evolution(),
        }

    def to_json_dict(self) -> Dict[str, Any]:
        """
        Generate all plots and return as JSON-serializable dictionaries.

        Returns:
            Dict mapping plot name to Plotly figure dict (fig.to_dict())
        """
        plots = self.get_all_plots()
        return {name: fig.to_dict() for name, fig in plots.items()}


def parse_and_plot(content: str) -> Tuple[Dict[str, Any], Dict[str, go.Figure]]:
    """
    Convenience function to parse messag content and generate all plots

    Args:
        content: Messag file content as string

    Returns:
        Tuple of (parsed_data, plots_dict)
    """
    parser = ConvergenceParser(content)
    data = parser.parse()

    summary = parser.get_summary()
    data["summary"] = summary

    plotter = ConvergencePlotter(data)
    plots = plotter.get_all_plots()

    return data, plots


def parse_and_plot_json(content: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Convenience function to parse messag content and return Plotly figures as JSON dicts.

    Suitable for FastAPI JSON responses — no Figure objects in the return value.

    Args:
        content: Messag file content as string

    Returns:
        Tuple of (summary_dict, plots_as_dicts)
        where plots_as_dicts maps plot name → Plotly figure dict
    """
    parser = ConvergenceParser(content)
    data = parser.parse()
    summary = parser.get_summary()

    plotter = ConvergencePlotter(data)
    plots_json = plotter.to_json_dict()

    return summary, plots_json


def compute_optimal_timestep(content: str) -> List[Tuple[float, float]]:
    """
    Derive an optimal timestep schedule from a completed simulation's messag file.

    The schedule lists only *change points* — entries where the timestep size
    differs from the previous one. Feeding this as a DTMAX load curve into the
    next run avoids wasting solver iterations on timestep sizes that are already
    known to fail at a given simulation time.

    Algorithm
    ---------
    1. Keep only converged timesteps.
    2. For each converged step compute:
           start_time = targetTime − stepSize
       (i.e. the simulation time at which this step size began)
    3. Emit (start_time, stepSize) only when stepSize changes relative to the
       previous emitted entry (plus always emit the very first entry).

    Args:
        content: Raw messag file content as a string.

    Returns:
        List of (start_time, dt) tuples, sorted by start_time.
        Empty list if no converged steps are found or targetTime/stepSize are
        missing.
    """
    parser = ConvergenceParser(content)
    data = parser.parse()

    converged = [
        s for s in data["timeSteps"]
        if s["convergenceStatus"] == "converged"
        and s["targetTime"] is not None
        and s["stepSize"] is not None
    ]

    if not converged:
        return []

    result: List[Tuple[float, float]] = []
    prev_dt: Optional[float] = None

    for step in converged:
        try:
            target = float(step["targetTime"])
            dt = float(step["stepSize"])
        except (ValueError, TypeError):
            continue

        start_time = target - dt

        # Emit only when dt changes (use a small tolerance for float safety)
        if prev_dt is None or abs(dt - prev_dt) > 1e-15 * max(dt, 1e-30):
            result.append((start_time, dt))
            prev_dt = dt

    return result


if __name__ == "__main__":
    sample_content = """
      Date: 01/15/2025      Time: 14:30:45
|  Hostname   : test-server
BEGIN implicit
      time = 1.000E-03  current step size = 1.000E-03
     Iteration: 1
     01/15/2025  14:30:46
      norm ratio = 1.234E-01  2.345E-02  3.456E-03
     Iteration: 2
     01/15/2025  14:30:47
      norm ratio = 5.678E-02  1.234E-02  2.345E-03
     Iteration: 3
     01/15/2025  14:30:48
      norm ratio = 2.345E-02  5.678E-03  1.234E-03
     Convergence detected
    """

    data, plots = parse_and_plot(sample_content)
    print(f"Parsed {len(data['timeSteps'])} timesteps")
    print(f"Generated {len(plots)} plots")

    summary, plots_json = parse_and_plot_json(sample_content)
    print(f"JSON summary: {summary}")
    print(f"JSON plots: {list(plots_json.keys())}")
