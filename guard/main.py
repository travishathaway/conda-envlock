from __future__ import annotations

import os
import logging
from pathlib import Path
from typing import NamedTuple
from collections.abc import Sequence

import click
from conda.base.context import context
from conda.base.constants import ROOT_ENV_NAME
from conda.core.envs_manager import list_all_known_prefixes
from conda.exceptions import CondaError
from conda.plugins import hookimpl, CondaPreCommand, CondaSubcommand
from rich import print as r_print
from rich.console import Console
from rich.table import Table

#: Name of the plugin; this will appear in certain outputs
PLUGIN_NAME = "conda_guard"

#: Name of the guard file that we create in environments themselves to signal they are guarded.
GUARDFILE_NAME = ".guarded"

#: Name of the command used when guarding/removing guards for environments
GUARD_COMMAND_NAME = "guard"

#: Name of the command used when listing guarded environments
GUARD_LIST_COMMAND_NAME = "glist"

#: Symbol we show for guarded environments
GUARDED_SYMBOL = "🔐"

#: Symbol we show for unguarded environments
UNGUARDED_SYMBOL = "🔓"

logger = logging.getLogger(PLUGIN_NAME)


class CondaGuardError(CondaError):
    """
    Error raised when we attempt to perform an action on a guarded environment
    """


class EnvironmentInfo(NamedTuple):
    name: str
    path: Path
    guarded: bool


def get_environment_info() -> list[EnvironmentInfo]:
    """
    Returns all environments currently known to conda.
    """
    prefixes = list_all_known_prefixes()
    name_to_prefix = get_name_to_prefix_map(prefixes)

    env_info = []

    for prefix in prefixes:
        if prefix not in name_to_prefix.values():
            path = Path(prefix)
            guardfile = path.joinpath(GUARDFILE_NAME)
            try:
                env_info.append(
                    EnvironmentInfo(name="", path=path, guarded=guardfile.exists())
                )
            except OSError as exc:
                logger.warning(
                    f"Could not determine if guard file '{guardfile}' exists: {exc}"
                )

    for name, prefix in name_to_prefix.items():
        path = Path(prefix)
        guardfile = path.joinpath(GUARDFILE_NAME)
        env_info.append(
            EnvironmentInfo(name=name, path=path, guarded=guardfile.exists())
        )

    return sorted(env_info, key=lambda env: env.name)


def toggle_environment_guard(env: EnvironmentInfo) -> EnvironmentInfo:
    """
    Toggles an environment to either a guarded or unguarded state. This function
    returns an updated `EnvironmentInfo` object.
    """
    if env.guarded:
        try:
            env.path.joinpath(GUARDFILE_NAME).unlink()
        except OSError as exc:
            raise CondaGuardError(
                f"Unable to remove a guard for the following reason: {exc}"
            )
    else:
        try:
            env.path.joinpath(GUARDFILE_NAME).touch()
        except OSError as exc:
            raise CondaGuardError(f"Unable to guard for the following reason: {exc}")

    return EnvironmentInfo(name=env.name, path=env.path, guarded=not env.guarded)


def get_name_to_prefix_map(prefixes: list[str]) -> dict[str, str]:
    """
    Retrieves a mapping name -> prefix

    TODO: What if there's duplicate names in multiple `envs_dirs`? 🤷‍
          Let's just pretend that never happens 🤫
    """
    mapping = {
        os.path.basename(prefix): prefix
        for prefix in prefixes
        for env_dir in context.envs_dirs
        if prefix.startswith(env_dir)
    }
    mapping[ROOT_ENV_NAME] = context.root_prefix

    return mapping


def get_prefix_to_name_map(prefixes: list[str]) -> dict[str, str]:
    """
    Retrieves a mapping of prefix -> name
    """
    mapping = {
        prefix: os.path.basename(prefix)
        for prefix in prefixes
        for env_dir in context.envs_dirs
        if prefix.startswith(env_dir)
    }
    mapping[context.root_prefix] = ROOT_ENV_NAME

    return mapping


def display_environment_info_table(environments: Sequence[EnvironmentInfo]) -> None:
    """
    Displays a rich table
    """
    table = Table(title="Conda Environments")

    table.add_column("Name", style="cyan")
    table.add_column("Prefix")
    table.add_column("Status")

    for row in environments:
        table.add_row(
            row.name or "-",
            str(row.path),
            f"{GUARDED_SYMBOL} [green]guarded" if row.guarded else "",
        )

    console = Console()
    console.print(table)


def validate_environment(ctx, param, value) -> EnvironmentInfo | None:
    """
    Makes sure that the environment passed in actually exists
    """
    # Construct two lists with known prefixes and environment names
    prefixes = list_all_known_prefixes()
    name_to_prefix = get_name_to_prefix_map(prefixes)

    if value not in prefixes:
        if value is not None:
            if value not in name_to_prefix.keys():
                raise CondaGuardError("Environment not found")
            path = Path(name_to_prefix[value])
            return EnvironmentInfo(
                name=value, path=path, guarded=path.joinpath(GUARDFILE_NAME).exists()
            )
        else:
            raise CondaGuardError("Please pass an environment name or prefix")

    path = Path(value)
    return EnvironmentInfo(
        name=value, path=path, guarded=path.joinpath(GUARDFILE_NAME).exists()
    )


@click.command("guard")
@click.argument("environment", callback=validate_environment, required=False)
def guard(environment):
    """
    Guard environments so changes are not accidentally made to them.

    This command works by placing a "guard-file" (.guarded) in the environment
    that to be guarded. Conda then scans the environment before running any
    actions on it, and if this guard-file exists, the actions are not run
    and the command exits early.
    """
    env = toggle_environment_guard(environment)
    guarded_or_unguarded = "[green]guarded" if env.guarded else "[magenta]unguarded"
    r_print(
        f"{env.name} is {GUARDED_SYMBOL if env.guarded else UNGUARDED_SYMBOL} "
        f"{guarded_or_unguarded}"
    )


@click.command("glist")
@click.option("--guarded", "-g", help="Only show guarded environments", is_flag=True)
@click.option("--named", "-n", help="Only show named environments", is_flag=True)
def glist(guarded, named):
    """
    List environments in conda and show whether they are guarded
    """
    all_environments = get_environment_info()

    if guarded:
        all_environments = [env for env in all_environments if env.guarded]

    if named:
        all_environments = [env for env in all_environments if env.name]

    display_environment_info_table(all_environments)


def guard_wrapper(args):
    """Guard environments so changes are not accidentally made to them"""
    guard(args=args, prog_name=f"conda {GUARD_COMMAND_NAME}", standalone_mode=False)


def glist_wrapper(args):
    """Lists guarded environments"""
    glist(
        args=args, prog_name=f"conda {GUARD_LIST_COMMAND_NAME}", standalone_mode=False
    )


def custom_plugin_pre_commands_action(command: str, parsed_args=None, raw_args=None):
    """
    Checks to see if the current environment being acted on is guarded and if so, raise error to
    exit program early

    TODO: This still doesn't handle `conda env update -f environment.yml`
          We will have to look inside the file and pluck out the environment name
    """
    known_envs = get_environment_info()

    if hasattr(parsed_args, "name") and parsed_args.name:
        lookup_attr = "name"
        value = parsed_args.name

    elif hasattr(parsed_args, "prefix") and parsed_args.prefix:
        lookup_attr = "path"
        value = Path(parsed_args.prefix)

    else:
        lookup_attr = "path"
        value = Path(context.active_prefix)

    # Create a list of guarded environments; length should be zero or one
    guarded_envs = [
        env for env in known_envs if getattr(env, lookup_attr) == value and env.guarded
    ]

    if guarded_envs:
        env = guarded_envs[0]
        raise CondaGuardError(
            f'Environment "{env.name or env.path}" is currently guarded. '
            f"Run `conda {GUARD_COMMAND_NAME} '{env.name or env.path}'` to remove guard it."
        )


@hookimpl
def conda_pre_commands():
    yield CondaPreCommand(
        name=f"{PLUGIN_NAME}_pre_command",
        action=custom_plugin_pre_commands_action,
        run_for={"install", "remove", "update", "info", "env_update", "env_remove"},
    )


@hookimpl
def conda_subcommands():
    yield CondaSubcommand(
        name=GUARD_COMMAND_NAME, action=guard_wrapper, summary=guard_wrapper.__doc__
    )
    yield CondaSubcommand(
        name=GUARD_LIST_COMMAND_NAME,
        action=glist_wrapper,
        summary=glist_wrapper.__doc__,
    )
