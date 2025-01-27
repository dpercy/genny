"""
Generates evergreen tasks based on the current state of the repo.
"""

import enum
import glob
import os
import re
from typing import NamedTuple, List, Optional, Set
import yaml
import structlog

from shrub.command import CommandDefinition
from shrub.config import Configuration
from shrub.variant import TaskSpec

from genny.cmd_runner import run_command

SLOG = structlog.get_logger(__name__)


#
# The classes are listed here in dependency order to avoid having to quote typenames.
#
# For comprehension, start at main(), then class Workload, then class Repo. Rest
# are basically just helpers.
#


class YamlReader:
    # You could argue that YamlReader, WorkloadLister, and maybe even Repo
    # should be the same class - perhaps renamed to System or something?
    # Maybe make these methods static to avoid having to pass an instance around.
    def load(self, workspace_root: str, path: str) -> dict:
        """
        :param workspace_root: effective cwd
        :param path: path relative to workspace_root
        :return: deserialized yaml file
        """
        joined = os.path.join(workspace_root, path)
        if not os.path.exists(joined):
            raise Exception(f"File {joined} not found.")
        with open(joined) as handle:
            return yaml.safe_load(handle)

    # Really just here for easy mocking.
    def exists(self, path: str) -> bool:
        return os.path.exists(path)

    def load_set(self, workspace_root: str, files: List[str]) -> dict:
        """
        :param workspace_root:
            effective cwd
        :param files:
            files to load relative to cwd
        :return:
            Key the basename (no extension) of the file and value the loaded contents.
            E.g. load_set("expansions") => {"expansions": {"contents":["of","expansions.yml"]}}
        """
        out = dict()
        for to_load in [f for f in files if self.exists(f)]:
            basename = str(os.path.basename(to_load).split(".yml")[0])
            out[basename] = self.load(workspace_root=workspace_root, path=to_load)
        return out


class WorkloadLister:
    """
    Lists files in the repo dir etc.
    Separate from the Repo class for easier testing.
    """

    def __init__(self, genny_repo_root: str, reader: YamlReader):
        self.genny_repo_root = genny_repo_root
        self._expansions = None
        self.reader = reader

    def all_workload_files(self) -> Set[str]:
        pattern = os.path.join(self.genny_repo_root, "src", "workloads", "**", "*.yml")
        return {*glob.glob(pattern)}

    def modified_workload_files(self) -> Set[str]:
        """Relies on git to find files in src/workloads modified versus origin/master"""
        command = (
            "git diff --name-only --diff-filter=AMR "
            "$(git merge-base HEAD origin/master) -- src/workloads/"
        )
        lines = run_command(cmd=[command], cwd=self.genny_repo_root, shell=True, check=True).stdout
        return {os.path.join(self.genny_repo_root, line) for line in lines if line.endswith(".yml")}


class OpName(enum.Enum):
    """
    What kind of tasks we're generating in this invocation.
    """

    ALL_TASKS = object()
    VARIANT_TASKS = object()
    PATCH_TASKS = object()


class CLIOperation(NamedTuple):
    """
    Represents the "input" to what we're doing"
    """

    mode: OpName
    variant: Optional[str]
    genny_repo_root: str
    workspace_root: str

    @staticmethod
    def create(
        mode_name: str, reader: YamlReader, genny_repo_root: str, workspace_root: str
    ) -> "CLIOperation":
        mode = OpName.ALL_TASKS
        variant = None

        if mode_name == "all_tasks":
            mode = OpName.ALL_TASKS
        if mode_name == "patch_tasks":
            mode = OpName.PATCH_TASKS
            variant = reader.load(workspace_root, "expansions.yml")["build_variant"]
        if mode_name == "variant_tasks":
            mode = OpName.VARIANT_TASKS
            variant = reader.load(workspace_root, "expansions.yml")["build_variant"]
        return CLIOperation(
            mode, variant, genny_repo_root=genny_repo_root, workspace_root=workspace_root
        )


class CurrentBuildInfo:
    def __init__(self, reader: YamlReader, workspace_root: str):
        self.conts = reader.load(workspace_root, "expansions.yml")

    def has(self, key: str, acceptable_values: List[str]) -> bool:
        """
        :param key: a key from environment (expansions.yml, bootstrap.yml, etc)
        :param acceptable_values: possible values we accept
        :return: if the actual value from env[key] is in the list of acceptable values
        """
        if key not in self.conts:
            raise Exception(f"Unknown key {key}. Know about {self.conts.keys()}")
        actual = self.conts[key]
        return any(actual == acceptable_value for acceptable_value in acceptable_values)


class GeneratedTask(NamedTuple):
    name: str
    mongodb_setup: Optional[str]
    workload: "Workload"


class Workload:
    """
    Represents a workload yaml file.
    Is a "child" object of Repo.
    """

    file_path: str
    """Path relative to repo root."""

    is_modified: bool

    requires: Optional[dict] = None
    """The `Requires` block, if present"""

    setups: Optional[List[str]] = None
    """The PrepareEnvironmentWith:mongodb_setup block, if any"""

    def __init__(self, workspace_root: str, file_path: str, is_modified: bool, reader: YamlReader):
        self.file_path = file_path
        self.is_modified = is_modified

        conts = reader.load(workspace_root, self.file_path)

        if "AutoRun" not in conts:
            return

        auto_run = conts["AutoRun"]
        self.requires = auto_run["Requires"]
        if "PrepareEnvironmentWith" in auto_run:
            prep = auto_run["PrepareEnvironmentWith"]
            if len(prep) != 1 or "mongodb_setup" not in prep:
                raise ValueError(
                    f"Need exactly mongodb_setup: [list] "
                    f"in PrepareEnvironmentWith for file {file_path}"
                )
            self.setups = prep["mongodb_setup"]

    @property
    def file_base_name(self) -> str:
        return str(os.path.basename(self.file_path).split(".yml")[0])

    @property
    def relative_path(self) -> str:
        return self.file_path.split("src/workloads/")[1]

    def all_tasks(self) -> List[GeneratedTask]:
        """
        :return: all possible tasks irrespective of the current build-variant etc.
        """
        base = self._to_snake_case(self.file_base_name)
        if self.setups is None:
            return [GeneratedTask(base, None, self)]
        return [
            GeneratedTask(f"{base}_{self._to_snake_case(setup)}", setup, self)
            for setup in self.setups
        ]

    def variant_tasks(self, build: CurrentBuildInfo) -> List[GeneratedTask]:
        """
        :param build: info about current build
        :return: tasks that we should do given the current build e.g. if we have Requires info etc.
        """
        if not self.requires:
            return []

        def meets_criteria() -> bool:
            okay = True
            for key, acceptable_values in self.requires.items():
                msg = "Scheduling workload."
                if not build.has(key, acceptable_values):
                    msg = "Not scheduling workload"
                    okay = False
                SLOG.info(
                    msg,
                    workload_base_name=self.file_base_name,
                    key=key,
                    acceptable_values=acceptable_values,
                    build_variant=build.conts.get("build_variant", "unknown"),
                )
            return okay

        return [task for task in self.all_tasks() if meets_criteria()]

    # noinspection RegExpAnonymousGroup
    @staticmethod
    def _to_snake_case(camel_case):
        """
        Converts CamelCase to snake_case, useful for generating test IDs
        https://stackoverflow.com/questions/1175208/
        :return: snake_case version of camel_case.
        """
        s1 = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", camel_case)
        s2 = re.sub("-", "_", s1)
        return re.sub("([a-z0-9])([A-Z])", r"\1_\2", s2).lower()


class Repo:
    """
    Represents the git checkout.
    """

    def __init__(self, lister: WorkloadLister, reader: YamlReader, workspace_root: str):
        self._modified_repo_files = None
        self.workspace_root = workspace_root
        self.lister = lister
        self.reader = reader

    def all_workloads(self) -> List[Workload]:
        all_files = self.lister.all_workload_files()
        modified = self.lister.modified_workload_files()
        return [
            Workload(
                workspace_root=self.workspace_root,
                file_path=fpath,
                is_modified=fpath in modified,
                reader=self.reader,
            )
            for fpath in all_files
        ]

    def modified_workloads(self) -> List[Workload]:
        return [workload for workload in self.all_workloads() if workload.is_modified]

    def all_tasks(self) -> List[GeneratedTask]:
        """
        :return: All possible tasks fom all possible workloads
        """
        # Double list-comprehensions always read backward to me :(
        return [task for workload in self.all_workloads() for task in workload.all_tasks()]

    def variant_tasks(self, build: CurrentBuildInfo):
        """
        :return: Tasks to schedule given the current variant (runtime)
        """
        return [task for workload in self.all_workloads() for task in workload.variant_tasks(build)]

    def patch_tasks(self) -> List[GeneratedTask]:
        """
        :return: Tasks for modified workloads current variant (runtime)
        """
        return [task for workload in self.modified_workloads() for task in workload.all_tasks()]

    def tasks(self, op: CLIOperation, build: CurrentBuildInfo) -> List[GeneratedTask]:
        """
        :param op: current cli invocation
        :param build: current build info
        :return: tasks that should be scheduled given the above
        """
        if op.mode == OpName.ALL_TASKS:
            tasks = self.all_tasks()
        elif op.mode == OpName.PATCH_TASKS:
            tasks = self.patch_tasks()
        elif op.mode == OpName.VARIANT_TASKS:
            tasks = self.variant_tasks(build)
        else:
            raise Exception("Invalid operation mode")
        return tasks


class ConfigWriter:
    """
    Takes tasks and converts them to shrub Configuration objects.
    """

    def __init__(self, op: CLIOperation):
        self.op = op

    def write(self, tasks: List[GeneratedTask], write: bool = True) -> Configuration:
        """
        :param tasks: tasks to write
        :param write: boolean to actually write the file - exposed for testing
        :return: the configuration object to write (exposed for testing)
        """
        if self.op.mode != OpName.ALL_TASKS:
            config: Configuration = self.variant_tasks(tasks, self.op.variant)
        else:
            config = self.all_tasks_modern(tasks)

        output_file = os.path.join(self.op.workspace_root, "build", "TaskJSON", "Tasks.json")

        success = False
        raised = None
        if write:
            try:
                out_text = config.to_json()
                os.makedirs(os.path.dirname(output_file), exist_ok=True)
                if os.path.exists(output_file):
                    os.unlink(output_file)
                with open(output_file, "w") as output:
                    output.write(out_text)
                    SLOG.debug("Wrote task json", output_file=output_file, contents=out_text)
                success = True
            except Exception as e:
                raised = e
                raise e
            finally:
                SLOG.info(
                    f"{'Succeeded' if success else 'Failed'} to write to {output_file} from cwd={os.getcwd()}."
                    f"{raised if raised else ''}"
                )
        return config

    @staticmethod
    def variant_tasks(tasks: List[GeneratedTask], variant: str) -> Configuration:
        c = Configuration()
        c.variant(variant).tasks([TaskSpec(task.name) for task in tasks])
        return c

    @staticmethod
    def all_tasks_modern(tasks: List[GeneratedTask]) -> Configuration:
        c = Configuration()
        c.exec_timeout(64800)  # 18 hours
        for task in tasks:
            bootstrap = {
                "test_control": task.name,
                "auto_workload_path": task.workload.relative_path,
            }
            if task.mongodb_setup:
                bootstrap["mongodb_setup"] = task.mongodb_setup

            t = c.task(task.name)
            t.priority(5)
            t.commands(
                [
                    CommandDefinition()
                    .command("timeout.update")
                    .params({"exec_timeout_secs": 86400, "timeout_secs": 7200}),  # 24 hours
                    CommandDefinition().function("f_run_dsi_workload").vars(bootstrap),
                ]
            )
        return c


def main(mode_name: str, genny_repo_root: str, workspace_root: str) -> None:
    reader = YamlReader()
    build = CurrentBuildInfo(reader=reader, workspace_root=workspace_root)
    op = CLIOperation.create(
        mode_name=mode_name,
        reader=reader,
        genny_repo_root=genny_repo_root,
        workspace_root=workspace_root,
    )
    lister = WorkloadLister(genny_repo_root=genny_repo_root, reader=reader)
    repo = Repo(lister=lister, reader=reader, workspace_root=workspace_root)
    tasks = repo.tasks(op=op, build=build)

    writer = ConfigWriter(op)
    writer.write(tasks)
