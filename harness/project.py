"""Where the project under test is.

THE ENGINE CONTAINS NO PROJECT DATA (PROJECT.md §2.7, PROJECT-V2 §8.2). Until
this file existed it contained something almost as bad: the ASSUMPTION that
there is exactly one project and that it lives at the repository root. Every
loader resolved `network.yml`, `catalog.yml`, `boards.yml`, `scenarios/` and
`patterns/` from the directory above `harness/`, so a second project could not
be opened without moving the first one out of the way.

This module is the one place that question is answered. It resolves a PROJECT
ROOT, which is a different thing from the repository root and must not be
confused with it:

    repository root     the engine, the scripts, the studio, the docs.
                        Where can_toolkit.py and toolchain-env.sh live, and
                        what a results file quotes paths relative to so the
                        file is machine-neutral.

    project root        network.yml, catalog.yml, boards.yml, patterns/,
                        scenarios/, platforms/, firmware/, dbc/, runs/, cache/.
                        Everything that is one customer's answer rather than
                        the tool's mechanism.

RESOLUTION ORDER, most explicit first:

    1. an explicit path      --project on the command line
    2. $BENCH_PROJECT        so a shell session, a script or CI can select one
    3. projects/demo-ev      the example project this repository ships with

A project directory that does not exist is REFUSED rather than defaulted. A
silent fallback would resolve every path against a directory with nothing in
it, and the first thing anyone would see is "no such scenario" -- which reads
as a missing test rather than as a missing project.
"""

import os
from pathlib import Path

_HERE = Path(__file__).resolve().parent

#: The repository: the engine and its tooling. NOT where project data lives.
REPO_ROOT = _HERE.parent

#: The project this repository ships as its worked example. It is a default,
#: not a hardcoding: every entry point takes --project, and $BENCH_PROJECT
#: overrides it for a whole session.
DEFAULT_PROJECT = "projects/demo-ev"

#: The environment variable the shell scripts and CI set.
PROJECT_ENV = "BENCH_PROJECT"

#: The files and directories a project is made of, as named in PROJECT-V2 §8.1.
#: Kept here so that no other module has to spell them, and so that adding one
#: is an edit to this list rather than a hunt through the engine.
NETWORK_FILE = "network.yml"
CATALOG_FILE = "catalog.yml"
BOARDS_FILE = "boards.yml"
PATTERN_DIR = "patterns"
SCENARIO_DIR = "scenarios"
RUNS_DIR = "runs"
#: Snapshots and the result cache (PROJECT-V2 8.1). Disposable by definition:
#: everything in it is derived from files that are hashed into its own keys, so
#: deleting it costs time and nothing else.
CACHE_DIR = "cache"


class ProjectError(Exception):
    """The project directory is missing or is not a project."""


def project_root(explicit=None, environ=None) -> Path:
    """The project under test, resolved once and the same way everywhere."""
    env = os.environ if environ is None else environ

    if explicit:
        chosen, why = Path(explicit), "--project"
    elif env.get(PROJECT_ENV):
        chosen, why = Path(env[PROJECT_ENV]), "$" + PROJECT_ENV
    else:
        chosen, why = REPO_ROOT / DEFAULT_PROJECT, "the default project"

    if not chosen.is_absolute():
        chosen = (REPO_ROOT / chosen)
    chosen = chosen.resolve()

    if not chosen.is_dir():
        raise ProjectError(
            "%s names %s, which is not a directory.\n"
            "  A project is a directory holding %s, %s, %s and the rest "
            "(PROJECT-V2 §8.1).\n"
            "  Pass --project <dir>, set %s, or use the example project at %s."
            % (why, chosen, NETWORK_FILE, CATALOG_FILE, BOARDS_FILE,
               PROJECT_ENV, DEFAULT_PROJECT)
        )
    return chosen


def network_path(explicit=None, project=None) -> Path:
    return Path(explicit) if explicit else project_root(project) / NETWORK_FILE


def catalog_path(explicit=None, project=None) -> Path:
    return Path(explicit) if explicit else project_root(project) / CATALOG_FILE


def boards_path(explicit=None, project=None) -> Path:
    return Path(explicit) if explicit else project_root(project) / BOARDS_FILE


def patterns_dir(explicit=None, project=None) -> Path:
    return Path(explicit) if explicit else project_root(project) / PATTERN_DIR


def scenarios_dir(explicit=None, project=None) -> Path:
    return Path(explicit) if explicit else project_root(project) / SCENARIO_DIR


def runs_dir(explicit=None, project=None) -> Path:
    return Path(explicit) if explicit else project_root(project) / RUNS_DIR


def cache_dir(explicit=None, project=None) -> Path:
    return Path(explicit) if explicit else project_root(project) / CACHE_DIR


def add_argument(parser) -> None:
    """Give a command line the same --project flag as every other one.

    One spelling, one help string, one default. Two entry points that disagreed
    about what --project means would be two definitions of where the data is.
    """
    parser.add_argument(
        "--project", default=None, metavar="DIR",
        help="the project directory to read (default: $%s, else %s)"
             % (PROJECT_ENV, DEFAULT_PROJECT),
    )
