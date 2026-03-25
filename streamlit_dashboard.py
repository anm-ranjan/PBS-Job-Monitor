"""
PBS Job Monitor Dashboard - Streamlit UI for monitoring LS-DYNA simulations

Provides real-time monitoring of PBS jobs across multiple Linux servers with:
- Job overview table with filtering
- Convergence norm plots (interactive Plotly charts)
- Live log streaming
- Path conversion between Windows and Linux file systems
"""

import os
import streamlit as st
import pandas as pd
import time
from datetime import datetime
from typing import Optional, Dict, Any, List
from collections import OrderedDict

from job_monitor import JobMonitor
from convergence_plotter import ConvergenceParser, ConvergencePlotter, parse_and_plot

st.set_page_config(
    page_title="ISHM-SUITE | PBS Job Monitor",
    page_icon="IPA_Logo.PNG",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom CSS styling inspired by IPA theme (blue-teal gradient)
CUSTOM_CSS = """
<style>
    /* Primary colors from IPA theme */
    :root {
        --primary-dark: #0a4d6e;
        --primary-blue: #1a6985;
        --primary-teal: #2d9ea8;
        --accent-light: #5dade2;
        --bg-light: #e8f6f8;
    }

    /* Sidebar styling */
    [data-testid="stSidebar"] {
        background: linear-gradient(180deg, #0a4d6e 0%, #1a6985 50%, #2d9ea8 100%);
    }

    [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p,
    [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] h1,
    [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] h2,
    [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] h3,
    [data-testid="stSidebar"] label,
    [data-testid="stSidebar"] .stSelectbox label,
    [data-testid="stSidebar"] span {
        color: white !important;
    }

    [data-testid="stSidebar"] hr {
        border-color: rgba(255, 255, 255, 0.3);
    }

    /* Header styling */
    .main h1 {
        color: #0a4d6e;
    }

    .main h2, .main h3 {
        color: #1a6985;
    }

    /* Metric styling */
    [data-testid="stMetricValue"] {
        color: #0a4d6e;
    }

    /* Button styling */
    .stButton > button[kind="primary"] {
        background-color: #2d9ea8;
        border-color: #2d9ea8;
    }

    .stButton > button[kind="primary"]:hover {
        background-color: #1a6985;
        border-color: #1a6985;
    }

    /* Tab styling */
    .stTabs [data-baseweb="tab-list"] button[aria-selected="true"] {
        background-color: #e8f6f8;
        color: #0a4d6e;
    }

    /* Info/warning box styling */
    .stAlert {
        border-radius: 8px;
    }

    /* Logo container styling */
    .logo-container {
        text-align: center;
        padding: 1rem 0;
        margin-bottom: 1rem;
    }

    .logo-container img {
        max-width: 120px;
        margin-bottom: 0.5rem;
    }

    .logo-text {
        color: white !important;
        font-size: 1.2rem;
        font-weight: bold;
        letter-spacing: 2px;
        margin-top: 0.5rem;
    }

    /* Dataframe header styling */
    [data-testid="stDataFrame"] th {
        background-color: #e8f6f8 !important;
        color: #0a4d6e !important;
    }
</style>
"""

st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

if "job_monitor" not in st.session_state:
    try:
        st.session_state.job_monitor = JobMonitor()
    except FileNotFoundError as e:
        st.error(f"Configuration error: {e}")
        st.error(
            "Please ensure config.yaml exists in the same directory as this script."
        )
        st.stop()

if "selected_job" not in st.session_state:
    st.session_state.selected_job = None

if "last_refresh" not in st.session_state:
    st.session_state.last_refresh = time.time()

if "plots_data" not in st.session_state:
    st.session_state.plots_data = None

if "show_kill_confirm" not in st.session_state:
    st.session_state.show_kill_confirm = False

if "action_in_progress" not in st.session_state:
    st.session_state.action_in_progress = False

if "show_delete_confirm" not in st.session_state:
    st.session_state.show_delete_confirm = False

if "job_to_delete" not in st.session_state:
    st.session_state.job_to_delete = None


def format_job_row(job: OrderedDict) -> Dict[str, str]:
    """Format job for display in dataframe"""
    return {
        "Server": job.get("Server", "N/A"),
        "JobID": job.get("JobID", "N/A"),
        "Job_Name": job.get("Job_Name", "N/A"),
        "Owner": job.get("Owner", "N/A"),
        "Status": job.get("Status", "N/A"),
        "CPUs": job.get("CPUs", "N/A"),
        "Memory": job.get("Memory", "N/A"),
        "Windows_Path": st.session_state.job_monitor.get_windows_path(job) or "N/A",
    }


def get_status_color(status: str) -> str:
    """Get color based on job status (IPA theme inspired)"""
    status_colors = {
        "R": "#2d9ea8",  # Running: Theme teal
        "Q": "#f39c12",  # Queued: Orange
        "C": "#2ecc71",  # Completed: Green
        "E": "#e74c3c",  # Error: Red
    }
    return status_colors.get(status, "#95a5a6")


def refresh_jobs():
    """Refresh job list from all servers"""
    monitor = st.session_state.job_monitor
    jobs = monitor.fetch_jobs()
    st.session_state.last_refresh = time.time()
    st.session_state.plots_data = None
    return jobs


def display_job_details(job: OrderedDict):
    """Display detailed information for selected job"""
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        st.metric("Job ID", job.get("JobID", "N/A"))

    with col2:
        st.metric("Server", job.get("Server", "N/A"))

    with col3:
        status = job.get("Status", "N/A")
        st.metric("Status", status)

    with col4:
        cpus = job.get("CPUs", "N/A")
        st.metric("CPUs", cpus)


def display_log_viewer(job: OrderedDict):
    """Display live log viewer with tail -f style output"""
    monitor = st.session_state.job_monitor

    st.subheader("Live Log Viewer")

    max_lines = st.slider("Tail Lines", 10, 500, 100, key="max_log_lines")

    messag_path = monitor.get_messag_path(job)
    if not messag_path or not os.path.exists(messag_path):
        st.error(f"Messag file not found: {messag_path}")
        return

    content, file_size = monitor.get_log_content(job, max_lines=max_lines)

    if content is None:
        st.error("Error reading log file")
        return

    if content:
        st.code(content, language=None)
        st.caption(f"Showing last {max_lines} lines (file size: {file_size:,} bytes)")
    else:
        st.info("Log file is empty")


def display_plots(content: str):
    """Display convergence plots"""
    if not content:
        st.warning("No convergence data available")
        return

    try:
        data, plots = parse_and_plot(content)
        st.session_state.plots_data = data

        plot_options = {
            "Iterations per Timestep": "iterations_per_timestep",
            "Time Duration": "time_duration",
            "Convergence Metrics": "convergence_metrics",
            "Convergence Status": "convergence_status",
            "Displacement Evolution": "displacement_evolution",
        }

        selected_plot = st.selectbox(
            "Select Plot", options=list(plot_options.keys()), key="plot_selector"
        )

        plot_key = plot_options[selected_plot]
        if plot_key in plots:
            st.plotly_chart(plots[plot_key], use_container_width=True)

        with st.expander("View Summary Statistics"):
            summary = data.get("summary", {})
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Total Steps", summary.get("total_steps", 0))
            col2.metric("Total Iterations", summary.get("total_iterations", 0))
            col3.metric("Converged Steps", summary.get("converged_steps", 0))
            col4.metric("Failed Steps", summary.get("failed_steps", 0))

            status = summary.get("termination_status", "unknown")
            st.write(f"**Termination Status:** {status}")

    except Exception as e:
        st.error(f"Error generating plots: {e}")


def main():
    st.title("PBS Job Monitor Dashboard")
    st.caption("ISHM-SUITE | LS-DYNA Simulation Monitoring")
    st.markdown("---")

    monitor = st.session_state.job_monitor

    sidebar = st.sidebar

    # Display logo and ISHM-SUITE branding
    logo_path = os.path.join(os.path.dirname(__file__), "IPA_Logo.PNG")
    if os.path.exists(logo_path):
        sidebar.image(logo_path, use_container_width=True)
    sidebar.markdown(
        '<p style="text-align: center; color: white; font-size: 1.3rem; '
        'font-weight: bold; letter-spacing: 3px; margin-top: -10px; margin-bottom: 20px;">'
        'ISHM-SUITE</p>',
        unsafe_allow_html=True,
    )
    sidebar.markdown("---")

    sidebar.header("Dashboard Settings")

    refresh_interval = sidebar.selectbox(
        "Refresh Interval",
        options=[0, 5, 10, 20, 30, 60],
        index=3,
        format_func=lambda x: "Manual" if x == 0 else f"{x}s",
    )

    auto_refresh = sidebar.checkbox("Auto-refresh", value=True, key="auto_refresh")

    sidebar.markdown("---")
    sidebar.header("Filters")

    servers = [s["name"] for s in monitor.get_servers()]
    selected_servers = sidebar.multiselect(
        "Filter by Server", options=servers, default=servers
    )

    status_filter = sidebar.multiselect(
        "Filter by Status",
        options=["R", "Q", "C", "E"],
        default=["R", "Q"],
        format_func=lambda x: {
            "R": "Running",
            "Q": "Queued",
            "C": "Completed",
            "E": "Error",
        }.get(x, x),
    )

    owner_filter = sidebar.text_input(
        "Filter by Owner",
        value="",
        placeholder="Enter username...",
        key="owner_filter",
    )

    sidebar.markdown("---")
    sidebar.header("Job Actions")

    with sidebar.expander("Submit New Job", expanded=False):
        submit_path = st.text_input(
            "Windows Path",
            placeholder="Z:\\path\\to\\job",
            key="submit_path",
        )
        submit_script = st.text_input(
            "Script Name",
            value="qsubrunfhgfs.sh",
            key="submit_script",
        )

        if st.button("Submit Job", key="submit_job_btn", type="primary"):
            if submit_path:
                st.session_state.action_in_progress = True
                with st.spinner("Submitting job..."):
                    success, message = monitor.submit_job(submit_path, submit_script)
                    st.session_state.action_in_progress = False
                    if success:
                        st.success(message)
                        time.sleep(5)
                        st.rerun()
                    else:
                        st.error(message)
            else:
                st.warning("Please enter a path")

    with sidebar.expander("Generate Report", expanded=False):
        report_path = st.text_input(
            "Windows Path",
            placeholder="Z:\\path\\to\\job",
            key="report_path",
        )
        launch_viewer = st.checkbox("Open viewer after generation", value=True, key="launch_viewer")

        if st.button("Generate Report", key="generate_report_btn", type="primary"):
            if report_path:
                st.session_state.action_in_progress = True

                # Start report generation
                with st.spinner("Starting report generation..."):
                    success, message = monitor.generate_report(report_path)

                if not success:
                    st.session_state.action_in_progress = False
                    st.error(message)
                else:
                    st.info(message)

                    # Poll for completion - check for start_server.sh or .cmd
                    status_placeholder = st.empty()
                    html_dir = os.path.join(report_path, "Simulation", "_HTML")
                    status_placeholder.text(f"Watching for: {html_dir}/start_server.*")

                    with st.spinner("Waiting for report generation to complete (up to 4 min)..."):
                        def update_status(elapsed):
                            status_placeholder.text(f"Elapsed: {int(elapsed)}s - Watching: {html_dir}")

                        complete, result_msg = monitor.wait_for_report_completion(
                            report_path,
                            timeout=240,
                            poll_interval=3.0,
                            progress_callback=update_status,
                        )

                    status_placeholder.empty()
                    st.session_state.action_in_progress = False

                    if complete:
                        st.success(result_msg)
                        # Launch viewer if requested
                        if launch_viewer:
                            with st.spinner("Launching report viewer..."):
                                view_success, view_message = monitor.launch_report_viewer(report_path)
                                if view_success:
                                    st.success(view_message)
                                else:
                                    st.warning(view_message)
                    else:
                        st.error(result_msg)
                        # Show helpful debug info
                        if os.path.isdir(html_dir):
                            files = os.listdir(html_dir)
                            st.info(f"Files in _HTML: {files[:10]}..." if len(files) > 10 else f"Files in _HTML: {files}")
                        else:
                            st.info(f"_HTML directory not found at: {html_dir}")
            else:
                st.warning("Please enter a path")

    sidebar.markdown("---")
    sidebar.header("Server Information")

    for server in monitor.get_servers():
        drive = monitor.server_to_drive.get(server["hostname"], "?")
        sidebar.text(f"{server['name']}: Drive {drive}")

    # Auto-refresh (disabled during actions)
    if refresh_interval > 0 and auto_refresh and not st.session_state.action_in_progress:
        try:
            from streamlit_autorefresh import st_autorefresh
            st_autorefresh(interval=refresh_interval * 1000, key="auto_refresh_timer")
        except ImportError:
            sidebar.caption("Install `streamlit-autorefresh` for auto-refresh")
    elif st.session_state.action_in_progress:
        sidebar.caption("Auto-refresh paused during action")

    jobs = refresh_jobs()

    if selected_servers:
        jobs = [j for j in jobs if j.get("Server") in selected_servers]

    if status_filter:
        status_map = {"Running": "R", "Queued": "Q", "Completed": "C", "Error": "E"}
        filter_codes = [status_map.get(s, s) for s in status_filter]
        jobs = [j for j in jobs if j.get("Status") in filter_codes]

    if owner_filter:
        jobs = [j for j in jobs if owner_filter.lower() in j.get("Owner", "").lower()]

    st.subheader(f"Jobs ({len(jobs)} total)")

    if not jobs:
        st.info("No jobs found matching current filters")
    else:
        df_data = [format_job_row(job) for job in jobs]
        df = pd.DataFrame(df_data)

        selected_idx = st.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
            selection_mode="single-row",
            on_select="rerun",
            key="job_table",
        )

        if selected_idx and len(selected_idx["selection"]["rows"]) > 0:
            idx = selected_idx["selection"]["rows"][0]
            if idx < len(jobs):
                selected_job = jobs[idx]
                st.session_state.selected_job = selected_job

    st.markdown("---")

    if st.session_state.selected_job:
        job = st.session_state.selected_job
        st.header(f"Job Details: {job.get('Job_Name', 'Unknown')}")

        display_job_details(job)

        # Job Actions
        st.subheader("Actions")
        action_col1, action_col2 = st.columns(2)

        with action_col1:
            if st.button("Kill Job", key="kill_job", type="primary"):
                st.session_state.show_kill_confirm = True

        with action_col2:
            pass  # Placeholder for future actions

        # Kill confirmation dialog
        if st.session_state.get("show_kill_confirm", False):
            st.warning(f"Are you sure you want to kill job {job.get('JobID')}?")
            confirm_col1, confirm_col2, _ = st.columns(3)

            with confirm_col1:
                if st.button("Yes, Kill Job", key="confirm_kill", type="primary"):
                    st.session_state.action_in_progress = True
                    st.session_state.show_kill_confirm = False

                    # Send qdel command
                    with st.spinner("Sending kill signal..."):
                        success, message = monitor.kill_job(job)

                    if not success:
                        st.session_state.action_in_progress = False
                        st.error(message)
                    else:
                        st.info(message)

                        # Poll for job termination (wait for job_log)
                        status_placeholder = st.empty()
                        with st.spinner("Waiting for job to terminate (up to 2 min)..."):
                            def update_status(elapsed):
                                status_placeholder.caption(f"Elapsed: {int(elapsed)}s")

                            terminated, term_msg = monitor.wait_for_job_termination(
                                job,
                                timeout=120,
                                poll_interval=2.0,
                                progress_callback=update_status,
                            )

                        status_placeholder.empty()
                        st.session_state.action_in_progress = False

                        if terminated:
                            st.success(term_msg)
                            # Now ask about deleting directory
                            st.session_state.show_delete_confirm = True
                            st.session_state.job_to_delete = job
                            st.rerun()
                        else:
                            st.warning(term_msg)
                            st.info("Job may still be terminating. Check again later.")
                            st.session_state.selected_job = None
                            time.sleep(2)
                            st.rerun()

            with confirm_col2:
                if st.button("Cancel", key="cancel_kill"):
                    st.session_state.show_kill_confirm = False
                    st.rerun()

        # Delete directory confirmation (shown after job is confirmed killed)
        if st.session_state.get("show_delete_confirm", False) and st.session_state.get("job_to_delete"):
            del_job = st.session_state.job_to_delete
            st.success(f"Job {del_job.get('JobID')} has been terminated.")
            st.warning("Do you want to delete the Simulation directory?")
            del_col1, del_col2, _ = st.columns(3)

            with del_col1:
                if st.button("Yes, Delete Directory", key="confirm_delete", type="primary"):
                    with st.spinner("Deleting simulation directory..."):
                        del_success, del_msg = monitor.delete_simulation_directory(del_job)
                        if del_success:
                            st.success(del_msg)
                        else:
                            st.error(del_msg)
                    st.session_state.show_delete_confirm = False
                    st.session_state.job_to_delete = None
                    st.session_state.selected_job = None
                    time.sleep(1)
                    st.rerun()

            with del_col2:
                if st.button("No, Keep Directory", key="cancel_delete"):
                    st.session_state.show_delete_confirm = False
                    st.session_state.job_to_delete = None
                    st.session_state.selected_job = None
                    st.rerun()

        tab1, tab2 = st.tabs(["Convergence Plots", "Live Log"])

        with tab1:
            if st.button("Refresh Plots", key="refresh_plots"):
                st.session_state.plots_data = None
                st.rerun()

            messag_path = monitor.get_messag_path(job)
            if messag_path and os.path.exists(messag_path):
                try:
                    with open(messag_path, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                    display_plots(content)
                except Exception as e:
                    st.error(f"Error reading messag file: {e}")
            else:
                st.warning(f"Messag file not found or not accessible: {messag_path}")

        with tab2:
            display_log_viewer(job)

    else:
        st.info("Select a job from the table above to view details")

    st.markdown("---")
    st.caption(
        f"Last refreshed: {datetime.fromtimestamp(st.session_state.last_refresh).strftime('%H:%M:%S')} | "
        f"Cache timeout: {monitor.cache_timeout}s"
    )


if __name__ == "__main__":
    main()
