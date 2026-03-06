import argparse
import contextlib
import subprocess
import sys
from collections import defaultdict
from datetime import date

from rich import box
from rich.columns import Columns
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()


def get_commit_dates(branch=None, author=None, all_branches=False, cwd=None):
    cmd = ["git", "log", "--format=%ad", "--date=short"]
    if all_branches:
        cmd.append("--all")
    elif branch:
        cmd.append(branch)
    if author:
        cmd.extend(["--author", author])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, cwd=cwd)
    except subprocess.CalledProcessError:
        console.print("[bold red]Error:[/] Could not read git log. Is the path a git repository?")
        sys.exit(1)

    dates = sorted({date.fromisoformat(line.strip()) for line in result.stdout.splitlines() if line.strip()})
    return dates


def get_diff_stats(branch=None, author=None, all_branches=False, cwd=None):
    """
    Two-pass approach:
    - --numstat for line insertions/deletions
    - --name-status for per-file A/M/D counts (commit-level granularity)
    Returns a dict mapping date -> {commits, insertions, deletions, added, modified, deleted}
    """
    base_args = []
    if all_branches:
        base_args.append("--all")
    elif branch:
        base_args.append(branch)
    if author:
        base_args.extend(["--author", author])

    def run(extra_fmt_args):
        cmd = ["git", "log", "--date=short"] + extra_fmt_args + base_args
        try:
            return subprocess.run(cmd, capture_output=True, text=True, check=True, cwd=cwd).stdout
        except subprocess.CalledProcessError:
            return ""

    stats = defaultdict(
        lambda: {
            "commits": 0,
            "insertions": 0,
            "deletions": 0,
            "added": 0,
            "deleted": 0,
        }
    )

    # Pass 1: line stats via --numstat
    current_date = None
    for line in run(["--numstat", "--format=COMMIT %ad"]).splitlines():
        if line.startswith("COMMIT "):
            current_date = date.fromisoformat(line[7:].strip())
            stats[current_date]["commits"] += 1
        elif current_date and line.strip():
            parts = line.split("\t")
            if len(parts) == 3:
                ins, dels, _ = parts
                stats[current_date]["insertions"] += int(ins) if ins.isdigit() else 0
                stats[current_date]["deletions"] += int(dels) if dels.isdigit() else 0

    # Pass 2: file-level A/M/D via --name-status
    current_date = None
    for line in run(["--name-status", "--format=COMMIT %ad"]).splitlines():
        if line.startswith("COMMIT "):
            current_date = date.fromisoformat(line[7:].strip())
        elif current_date and line.strip():
            parts = line.split("\t")
            status = parts[0][0] if parts else ""  # first char: A/M/D/R/C/…
            if status == "A":
                stats[current_date]["added"] += 1
            elif status == "D":
                stats[current_date]["deleted"] += 1
            # R (rename) and C (copy) intentionally ignored

    return stats


def get_tag_dates(cwd=None):
    """
    Returns a dict mapping date -> [tag_name, ...] for all tags in the repo.
    Uses creatordate so annotated tags use the tag date, lightweight tags use commit date.
    """
    cmd = ["git", "tag", "--sort=creatordate", "--format=%(refname:short)	%(creatordate:short)"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, cwd=cwd)
    except subprocess.CalledProcessError:
        return {}

    tag_map = defaultdict(list)
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or "\t" not in line:
            continue
        name, datestr = line.split("\t", 1)
        with contextlib.suppress(ValueError):
            tag_map[date.fromisoformat(datestr)].append(name)
    return tag_map


def group_into_sessions(dates, gap_days):
    """
    Groups a sorted list of dates into sessions. A new session starts when
    the gap between two consecutive active dates exceeds `gap_days`.
    """
    if not dates:
        return []

    sessions = []
    session_dates = [dates[0]]

    for prev, curr in zip(dates, dates[1:], strict=False):
        if (curr - prev).days <= gap_days:
            session_dates.append(curr)
        else:
            sessions.append(session_dates)
            session_dates = [curr]

    sessions.append(session_dates)
    return sessions


def density_bar_slim(active_days, calendar_days, width=12):
    filled = round((active_days / calendar_days) * width) if calendar_days else 0
    bar = "█" * filled + "░" * (width - filled)
    return f"[green]{bar}[/green]"


def fmt_lines(n):
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def print_report(sessions, gap_days, diff_stats, tag_dates, show_dates=False):
    total_active_days = sum(len(s) for s in sessions)
    project_start = sessions[0][0]
    project_end = sessions[-1][-1]
    calendar_days = (project_end - project_start).days + 1

    # Aggregate per-session diff stats
    session_stats = []
    for session in sessions:
        commits = sum(diff_stats[d]["commits"] for d in session)
        ins = sum(diff_stats[d]["insertions"] for d in session)
        dels = sum(diff_stats[d]["deletions"] for d in session)
        added = sum(diff_stats[d]["added"] for d in session)
        deleted = sum(diff_stats[d]["deleted"] for d in session)
        session_stats.append(
            {
                "commits": commits,
                "ins": ins,
                "dels": dels,
                "added": added,
                "deleted": deleted,
            }
        )

    # Tags per session: collect any tag whose date falls within session date range
    session_tags = []
    for session in sessions:
        start, end = session[0], session[-1]
        tags = [t for d, ts in tag_dates.items() if start <= d <= end for t in ts]
        session_tags.append(tags)

    total_commits = sum(s["commits"] for s in session_stats)
    total_ins = sum(s["ins"] for s in session_stats)
    total_dels = sum(s["dels"] for s in session_stats)
    total_added = sum(s["added"] for s in session_stats)
    total_deleted = sum(s["deleted"] for s in session_stats)

    # ── Summary panel ──────────────────────────────────────────────────────
    summary = (
        f"[bold]Project span[/]   {project_start}  →  {project_end}  "
        f"[dim]({calendar_days} calendar days)[/dim]\n"
        f"[bold]Active days[/]    [cyan]{total_active_days}[/cyan] days across "
        f"[cyan]{len(sessions)}[/cyan] session(s)  "
        f"[dim](gap threshold: {gap_days} days)[/dim]\n"
        f"[bold]Total churn[/]    [green]+{fmt_lines(total_ins)}[/green]  "
        f"[red]-{fmt_lines(total_dels)}[/red]  "
        f"[dim]across {total_commits} commit(s)[/dim]"
    )
    console.print(Panel(summary, title="[bold cyan]Git Active Days[/bold cyan]", expand=False))

    # ── Session table ───────────────────────────────────────────────────────
    # Column order: # | Idle days | Start | End | Cal days | Active days | Activity | Commits | +files | -files | +lines | -lines | Milestones  # noqa: E501
    table = Table(box=box.ROUNDED, show_header=True, header_style="bold cyan", expand=False)
    table.add_column("#", justify="right", style="dim", width=4)
    table.add_column("Idle days", justify="right", style="dim", width=10)
    table.add_column("Start", justify="center", width=12)
    table.add_column("End", justify="center", width=12)
    table.add_column("Cal days", justify="right", style="dim", width=9)
    table.add_column("Active days", justify="right", style="cyan", width=11)
    table.add_column("Activity", justify="left", width=14)
    table.add_column("Commits", justify="right", style="dim", width=8)
    table.add_column("+files", justify="right", style="green", width=7)
    table.add_column("-files", justify="right", style="red", width=7)
    table.add_column("+lines", justify="right", style="green", width=8)
    table.add_column("-lines", justify="right", style="red", width=8)
    table.add_column("Milestones", justify="left", width=24)

    prev_end = None
    for i, (session, stats) in enumerate(zip(sessions, session_stats, strict=True), 1):
        start, end = session[0], session[-1]
        active = len(session)
        cal = (end - start).days + 1
        idle = f"{(start - prev_end).days - 1}" if prev_end else "[dim]—[/dim]"
        table.add_row(
            str(i),
            idle,
            str(start),
            str(end),
            str(cal),
            str(active),
            density_bar_slim(active, cal),
            str(stats["commits"]),
            f"+{stats['added']}",
            f"-{stats['deleted']}",
            f"+{fmt_lines(stats['ins'])}",
            f"-{fmt_lines(stats['dels'])}",
            "[magenta]" + ", ".join(session_tags[i - 1]) + "[/magenta]" if session_tags[i - 1] else "[dim]—[/dim]",
        )
        prev_end = end

    total_cal_days = sum((s[-1] - s[0]).days + 1 for s in sessions)
    total_idle = sum((sessions[i][0] - sessions[i - 1][-1]).days - 1 for i in range(1, len(sessions)))
    total_pct = round(100 * total_active_days / total_cal_days) if total_cal_days else 0
    table.add_section()
    table.add_row(
        "[bold]Σ[/bold]",
        f"[bold dim]{total_idle}[/bold dim]",
        "",
        "",
        f"[bold]{total_cal_days}[/bold]",
        f"[bold cyan]{total_active_days}[/bold cyan]",
        f"[dim]{total_pct:>3}%[/dim]",
        f"[bold]{total_commits}[/bold]",
        f"[bold green]+{total_added}[/bold green]",
        f"[bold red]-{total_deleted}[/bold red]",
        f"[bold green]+{fmt_lines(total_ins)}[/bold green]",
        f"[bold red]-{fmt_lines(total_dels)}[/bold red]",
        "",
    )

    console.print(table)

    # ── Per-session date chips (opt-in) ────────────────────────────────────
    if show_dates:
        for i, session in enumerate(sessions, 1):
            chips = [Text(f" {d} ", style="on dark_blue") for d in session]
            console.print(f"\n  [dim]Session {i} dates:[/dim]")
            console.print(Columns(chips, equal=False, expand=False, padding=(0, 1)))

    console.print()


def main():
    parser = argparse.ArgumentParser(
        description="Estimate active working days from git commit history.",
        epilog=(
            "Examples:\n"
            "  git-active-days                          # run in current repo\n"
            "  git-active-days /path/to/repo            # specify repo path\n"
            "  git-active-days --gap 14                 # 14-day session gap\n"
            "  git-active-days --branch main            # single branch\n"
            "  git-active-days --author 'Habib'         # filter by author\n"
            "  git-active-days --all-branches           # all branches\n"
            "  git-active-days --dates                  # show active dates\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--gap", type=int, default=7, help="Days of inactivity that signal a new session (default: 7)")
    parser.add_argument("--branch", type=str, default=None, help="Branch to analyze (default: current branch)")
    parser.add_argument("--author", type=str, default=None, help="Filter commits by author name or email")
    parser.add_argument("--all-branches", action="store_true", help="Analyze commits across all branches (combine with --author on shared repos to avoid inflated counts)")
    parser.add_argument("--dates", action="store_true", help="Print individual active dates for each session")
    parser.add_argument("path", nargs="?", default=None, help="Path to git repository (default: current directory)")
    args = parser.parse_args()

    cwd = args.path or None

    dates = get_commit_dates(
        branch=args.branch,
        author=args.author,
        all_branches=args.all_branches,
        cwd=cwd,
    )

    if not dates:
        console.print("[yellow]No commits found matching your filters.[/yellow]")
        sys.exit(0)

    diff_stats = get_diff_stats(
        branch=args.branch,
        author=args.author,
        all_branches=args.all_branches,
        cwd=cwd,
    )

    tag_dates = get_tag_dates(cwd=cwd)
    sessions = group_into_sessions(dates, gap_days=args.gap)
    print_report(sessions, gap_days=args.gap, diff_stats=diff_stats, tag_dates=tag_dates, show_dates=args.dates)


if __name__ == "__main__":
    main()
