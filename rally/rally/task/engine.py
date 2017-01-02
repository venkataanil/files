# Copyright 2013: Mirantis Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import copy
import httplib2
import json
import jsonschema
import os
import pstats
import requests
import six
import socket
import threading
import time
import traceback

from oslo_utils import fileutils
from six.moves import http_client as httplib


from rally.common.i18n import _
from rally.common import logging
from rally.common import objects
from rally.common import utils
from rally import consts
from rally import exceptions
from rally import osclients
from rally.plugins.openstack.context.keystone import existing_users
from rally.plugins.openstack.context.keystone import users as users_ctx
from rally.task import context
from rally.task import hook
from rally.task import runner
from rally.task import scenario
from rally.task import sla


LOG = logging.getLogger(__name__)

sock_path = '/tmp/sock'
pids = ['28776', '28777']


def set_socket_path(path):
    global sock_path
    sock_path = path


def get_socket_path():
    global sock_path
    return sock_path


class UnixDomainHTTPConnection(httplib.HTTPConnection):
    """Connection class for HTTP over UNIX domain socket."""
    def __init__(self, host, port=None, strict=None, timeout=None,
                 proxy_info=None):
        httplib.HTTPConnection.__init__(self, host, port, strict)
        self.timeout = timeout

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if self.timeout:
            self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class KeepalivedUnixDomainConnection(UnixDomainHTTPConnection):
    def __init__(self, *args, **kwargs):
        # Old style super initialization is required!
        UnixDomainHTTPConnection.__init__(
            self, *args, **kwargs)
        self.socket_path = get_socket_path()


def notify_agent(file_name, action):
    resp, content = httplib2.Http().request(
    # Note that the message is sent via a Unix domain socket so that
    # the URL doesn't matter.
        'http://127.0.0.1/',
        headers={'X-Neutron-Profiler-Filename': file_name,
                 'X-Neutron-Profiler-Action': action},
        connection_type=KeepalivedUnixDomainConnection)

    if resp.status != requests.codes.ok:
        raise Exception(_('Unexpected response: %s') % resp)


def set_trace(action, file_name):
    global pids
    for pid in pids:
        sock = os.path.join('/tmp', pid, 'sock')
        set_socket_path(sock)
        file_path = os.path.join('/tmp', pid, file_name)
        notify_agent(file_path, action)


def write_trace_to_file(taskid):
    global pids
    pid_files = []
    for pid in pids:
        pid_files.append(os.path.join('/tmp', pid, taskid))

    tracefile = os.path.join("/tmp/rally/", taskid, "calltrace")
    fileutils.ensure_tree(os.path.dirname(tracefile), mode=0o755)
    with open(tracefile, "w+") as f:
        for position, item in enumerate(pid_files):
            if position == 0:
                stats = pstats.Stats(item, stream=f)
            else:
                stats.add(item)
        stats.sort_stats('cumulative')
        stats.print_stats('neutron')


class ResultConsumer(object):
    """ResultConsumer class stores results from ScenarioRunner, checks SLA.

    Also ResultConsumer listens for runner events and notifies HookExecutor
    about started iterations.
    """

    def __init__(self, key, task, subtask, workload, runner,
                 abort_on_sla_failure):
        """ResultConsumer constructor.

        :param key: Scenario identifier
        :param task: Instance of Task, task to run
        :param subtask: Instance of Subtask
        :param workload: Instance of Workload
        :param runner: ScenarioRunner instance that produces results to be
                       consumed
        :param abort_on_sla_failure: True if the execution should be stopped
                                     when some SLA check fails
        """

        self.key = key
        self.task = task
        self.subtask = subtask
        self.workload = workload
        self.runner = runner
        self.load_started_at = float("inf")
        self.load_finished_at = 0

        self.sla_checker = sla.SLAChecker(key["kw"])
        self.hook_executor = hook.HookExecutor(key["kw"], self.task)
        self.abort_on_sla_failure = abort_on_sla_failure
        self.is_done = threading.Event()
        self.unexpected_failure = {}
        self.results = []
        self.thread = threading.Thread(target=self._consume_results)
        self.aborting_checker = threading.Thread(target=self.wait_and_abort)
        if "hooks" in self.key["kw"]:
            self.event_thread = threading.Thread(target=self._consume_events)

    def __enter__(self):
        self.thread.start()
        self.aborting_checker.start()
        if "hooks" in self.key["kw"]:
            self.event_thread.start()
        self.start = time.time()
        set_trace('start_trace', self.task["uuid"])
        return self

    def _consume_results(self):
        task_aborted = False
        while True:
            if self.runner.result_queue:
                results = self.runner.result_queue.popleft()
                self.results.extend(results)
                for r in results:
                    self.load_started_at = min(r["timestamp"],
                                               self.load_started_at)
                    self.load_finished_at = max(r["duration"] + r["timestamp"],
                                                self.load_finished_at)
                    success = self.sla_checker.add_iteration(r)
                    if (self.abort_on_sla_failure and
                            not success and
                            not task_aborted):
                        self.sla_checker.set_aborted_on_sla()
                        self.runner.abort()
                        self.task.update_status(
                            consts.TaskStatus.SOFT_ABORTING)
                        task_aborted = True
            elif self.is_done.isSet():
                break
            else:
                time.sleep(0.1)

    def _consume_events(self):
        while not self.is_done.isSet() or self.runner.event_queue:
            if self.runner.event_queue:
                event = self.runner.event_queue.popleft()
                self.hook_executor.on_event(
                    event_type=event["type"], value=event["value"])
            else:
                time.sleep(0.01)

    def __exit__(self, exc_type, exc_value, exc_traceback):
        self.finish = time.time()
        self.is_done.set()
        self.aborting_checker.join()
        self.thread.join()

        if exc_type:
            self.sla_checker.set_unexpected_failure(exc_value)

        if objects.Task.get_status(
                self.task["uuid"]) == consts.TaskStatus.ABORTED:
            self.sla_checker.set_aborted_manually()

        # NOTE(boris-42): Sort in order of starting instead of order of ending
        self.results.sort(key=lambda x: x["timestamp"])

        load_duration = max(self.load_finished_at - self.load_started_at, 0)

        LOG.info("Load duration is: %s" % utils.format_float_to_str(
            load_duration))
        LOG.info("Full runner duration is: %s" %
                 utils.format_float_to_str(self.runner.run_duration))
        LOG.info("Full duration is %s" % utils.format_float_to_str(
            self.finish - self.start))

        results = {
            "load_duration": load_duration,
            "full_duration": self.finish - self.start,
            "sla": self.sla_checker.results(),
        }
        self.runner.send_event(type="load_finished", value=results)
        if "hooks" in self.key["kw"]:
            self.event_thread.join()
            results["hooks"] = self.hook_executor.results()
        set_trace('stop_trace', self.task["uuid"])
        write_trace_to_file(self.task["uuid"])
        self.workload.add_workload_data({"raw": self.results})
        self.workload.set_results(results)

    @staticmethod
    def is_task_in_aborting_status(task_uuid, check_soft=True):
        """Checks task is in abort stages

        :param task_uuid: UUID of task to check status
        :type task_uuid: str
        :param check_soft: check or not SOFT_ABORTING status
        :type check_soft: bool
        """
        stages = [consts.TaskStatus.ABORTING, consts.TaskStatus.ABORTED]
        if check_soft:
            stages.append(consts.TaskStatus.SOFT_ABORTING)
        return objects.Task.get_status(task_uuid) in stages

    def wait_and_abort(self):
        """Waits until abort signal is received and aborts runner in this case.

        Has to be run from different thread simultaneously with the
        runner.run method.
        """

        while not self.is_done.isSet():
            if self.is_task_in_aborting_status(self.task["uuid"],
                                               check_soft=False):
                self.runner.abort()
                self.task.update_status(consts.TaskStatus.ABORTED)
                break
            time.sleep(2.0)


class TaskEngine(object):
    """The Task engine class is used to execute benchmark scenarios.

    An instance of this class is initialized by the API with the task
    configuration and then is used to validate and execute all specified
    in config subtasks.

    .. note::

        Typical usage:
            ...
            admin = ....  # contains dict representations of objects.Credential
                          # with OpenStack admin credentials

            users = ....  # contains a list of dicts of representations of
                          # objects.Credential with OpenStack users credentials

            engine = TaskEngine(config, task, admin=admin, users=users)
            engine.validate()   # to test config
            engine.run()        # to run config
    """

    def __init__(self, config, task, admin=None, users=None,
                 abort_on_sla_failure=False):
        """TaskEngine constructor.

        :param config: Dict with configuration of specified benchmark scenarios
        :param task: Instance of Task,
                     the current task which is being performed
        :param admin: Dict with admin credentials
        :param users: List of dicts with user credentials
        :param abort_on_sla_failure: True if the execution should be stopped
                                     when some SLA check fails
        """
        try:
            self.config = TaskConfig(config)
        except Exception as e:
            task.set_failed(type(e).__name__,
                            str(e),
                            json.dumps(traceback.format_exc()))
            raise exceptions.InvalidTaskException(str(e))

        self.task = task
        self.admin = admin and objects.Credential(**admin) or None
        self.existing_users = users or []
        self.abort_on_sla_failure = abort_on_sla_failure

    @logging.log_task_wrapper(LOG.info, _("Task validation check cloud."))
    def _check_cloud(self):
        clients = osclients.Clients(self.admin)
        clients.verified_keystone()

    @logging.log_task_wrapper(LOG.info,
                              _("Task validation of scenarios names."))
    def _validate_config_scenarios_name(self, config):
        available = set(s.get_name() for s in scenario.Scenario.get_all())

        specified = set()
        for subtask in config.subtasks:
            for s in subtask.workloads:
                specified.add(s.name)

        if not specified.issubset(available):
            names = ", ".join(specified - available)
            raise exceptions.NotFoundScenarios(names=names)

    @logging.log_task_wrapper(LOG.info, _("Task validation of syntax."))
    def _validate_config_syntax(self, config):
        for subtask in config.subtasks:
            for pos, workload in enumerate(subtask.workloads):
                try:
                    runner.ScenarioRunner.validate(workload.runner)
                    context.ContextManager.validate(
                        workload.context, non_hidden=True)
                    sla.SLA.validate(workload.sla)
                    for hook_conf in workload.hooks:
                        hook.Hook.validate(hook_conf)
                except (exceptions.RallyException,
                        jsonschema.ValidationError) as e:

                    kw = workload.make_exception_args(
                        pos, six.text_type(e))
                    raise exceptions.InvalidTaskConfig(**kw)

    def _validate_config_semantic_helper(self, admin, user, workload, pos,
                                         deployment):
        try:
            scenario.Scenario.validate(
                workload.name, workload.to_dict(),
                admin=admin, users=[user], deployment=deployment)
        except exceptions.InvalidScenarioArgument as e:
            kw = workload.make_exception_args(pos, six.text_type(e))
            raise exceptions.InvalidTaskConfig(**kw)

    def _get_user_ctx_for_validation(self, ctx):
        if self.existing_users:
            ctx["config"] = {"existing_users": self.existing_users}
            user_context = existing_users.ExistingUsers(ctx)
        else:
            user_context = users_ctx.UserGenerator(ctx)

        return user_context

    @logging.log_task_wrapper(LOG.info, _("Task validation of semantic."))
    def _validate_config_semantic(self, config):
        self._check_cloud()

        ctx_conf = {"task": self.task, "admin": {"credential": self.admin}}
        deployment = objects.Deployment.get(self.task["deployment_uuid"])

        # TODO(boris-42): It's quite hard at the moment to validate case
        #                 when both user context and existing_users are
        #                 specified. So after switching to plugin base
        #                 and refactoring validation mechanism this place
        #                 will be replaced
        with self._get_user_ctx_for_validation(ctx_conf) as ctx:
            ctx.setup()
            admin = osclients.Clients(self.admin)
            user = osclients.Clients(ctx_conf["users"][0]["credential"])

            for u in ctx_conf["users"]:
                user = osclients.Clients(u["credential"])
                for subtask in config.subtasks:
                    for pos, workload in enumerate(subtask.workloads):
                        self._validate_config_semantic_helper(
                            admin, user, workload,
                            pos, deployment)

    @logging.log_task_wrapper(LOG.info, _("Task validation."))
    def validate(self):
        """Perform full task configuration validation."""
        self.task.update_status(consts.TaskStatus.VERIFYING)
        try:
            self._validate_config_scenarios_name(self.config)
            self._validate_config_syntax(self.config)
            self._validate_config_semantic(self.config)
        except Exception as e:
            exception_info = json.dumps(traceback.format_exc(), indent=2)
            self.task.set_failed(type(e).__name__,
                                 str(e), exception_info)
            LOG.debug(exception_info)
            raise exceptions.InvalidTaskException(str(e))

    def _get_runner(self, config):
        config = config or {"type": "serial"}
        return runner.ScenarioRunner.get(config["type"])(self.task, config)

    def _prepare_context(self, ctx, name, credential):
        scenario_context = copy.deepcopy(
            scenario.Scenario.get(name)._meta_get("default_context"))
        if self.existing_users and "users" not in ctx:
            scenario_context.setdefault("existing_users", self.existing_users)
        elif "users" not in ctx:
            scenario_context.setdefault("users", {})

        scenario_context.update(ctx)
        context_obj = {
            "task": self.task,
            "admin": {"credential": credential},
            "scenario_name": name,
            "config": scenario_context
        }

        return context_obj

    @logging.log_task_wrapper(LOG.info, _("Benchmarking."))
    def run(self):
        """Run the benchmark according to the test configuration.

        Test configuration is specified on engine initialization.

        :returns: List of dicts, each dict containing the results of all the
                  corresponding benchmark test launches
        """
        self.task.update_status(consts.TaskStatus.RUNNING)

        for subtask in self.config.subtasks:
            subtask_obj = self.task.add_subtask(**subtask.to_dict())

            for pos, workload in enumerate(subtask.workloads):

                if ResultConsumer.is_task_in_aborting_status(
                        self.task["uuid"]):
                    LOG.info("Received aborting signal.")
                    self.task.update_status(consts.TaskStatus.ABORTED)
                    return

                key = workload.make_key(pos)
                workload_obj = subtask_obj.add_workload(key)
                LOG.info("Running benchmark with key: \n%s"
                         % json.dumps(key, indent=2))
                runner_obj = self._get_runner(workload.runner)
                context_obj = self._prepare_context(
                    workload.context, workload.name, self.admin)
                try:
                    with ResultConsumer(key, self.task,
                                        subtask_obj, workload_obj, runner_obj,
                                        self.abort_on_sla_failure):
                        with context.ContextManager(context_obj):
                            runner_obj.run(workload.name, context_obj,
                                           workload.args)
                except Exception as e:
                    LOG.debug(traceback.format_exc())
                    LOG.exception(e)

        if objects.Task.get_status(
                self.task["uuid"]) != consts.TaskStatus.ABORTED:
            self.task.update_status(consts.TaskStatus.FINISHED)


class TaskConfig(object):
    """Version-aware wrapper around task.

    """

    HOOK_CONFIG = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "description": {"type": "string"},
            "args": {},
            "trigger": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "args": {},
                },
                "required": ["name", "args"],
                "additionalProperties": False,
            }
        },
        "required": ["name", "args", "trigger"],
        "additionalProperties": False,
    }

    CONFIG_SCHEMA_V1 = {
        "type": "object",
        "$schema": consts.JSON_SCHEMA,
        "patternProperties": {
            ".*": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "args": {"type": "object"},
                        "runner": {
                            "type": "object",
                            "properties": {"type": {"type": "string"}},
                            "required": ["type"]
                        },
                        "context": {"type": "object"},
                        "sla": {"type": "object"},
                        "hooks": {
                            "type": "array",
                            "items": HOOK_CONFIG,
                        }
                    },
                    "additionalProperties": False
                }
            }
        }
    }

    CONFIG_SCHEMA_V2 = {
        "type": "object",
        "$schema": consts.JSON_SCHEMA,
        "properties": {
            "version": {"type": "number"},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "tags": {
                "type": "array",
                "items": {"type": "string"}
            },

            "subtasks": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "group": {"type": "string"},
                        "description": {"type": "string"},
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"}
                        },

                        "run_in_parallel": {"type": "boolean"},
                        "workloads": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 1,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "args": {"type": "object"},

                                    "runner": {
                                        "type": "object",
                                        "properties": {
                                            "type": {"type": "string"}
                                        },
                                        "required": ["type"]
                                    },

                                    "sla": {"type": "object"},
                                    "hooks": {
                                        "type": "array",
                                        "items": HOOK_CONFIG,
                                    },
                                    "context": {"type": "object"}
                                },
                                "additionalProperties": False,
                                "required": ["name", "runner"]
                            }
                        }
                    },
                    "additionalProperties": False,
                    "required": ["title", "workloads"]
                }
            }
        },
        "additionalProperties": False,
        "required": ["title", "subtasks"]
    }

    CONFIG_SCHEMAS = {1: CONFIG_SCHEMA_V1, 2: CONFIG_SCHEMA_V2}

    def __init__(self, config):
        """TaskConfig constructor.

        :param config: Dict with configuration of specified task
        """
        if config is None:
            # NOTE(stpierre): This gets reraised as
            # InvalidTaskException. if we raise it here as
            # InvalidTaskException, then "Task config is invalid: "
            # gets prepended to the message twice.
            raise Exception(_("Input task is empty"))

        self.version = self._get_version(config)
        self._validate_version()
        self._validate_json(config)

        self.title = config.get("title", "Task")
        self.tags = config.get("tags", [])
        self.description = config.get("description")

        self.subtasks = self._make_subtasks(config)

        # if self.version == 1:
        # TODO(ikhudoshyn): Warn user about deprecated format

    @staticmethod
    def _get_version(config):
        return config.get("version", 1)

    def _validate_version(self):
        if self.version not in self.CONFIG_SCHEMAS:
            allowed = ", ".join([str(k) for k in self.CONFIG_SCHEMAS])
            msg = (_("Task configuration version {0} is not supported. "
                     "Supported versions: {1}")).format(self.version, allowed)
            raise exceptions.InvalidTaskException(msg)

    def _validate_json(self, config):
        try:
            jsonschema.validate(config, self.CONFIG_SCHEMAS[self.version])
        except Exception as e:
            raise exceptions.InvalidTaskException(str(e))

    def _make_subtasks(self, config):
        if self.version == 2:
            return [SubTask(s) for s in config["subtasks"]]
        elif self.version == 1:
            subtasks = []
            for name, v1_workloads in config.items():
                for v1_workload in v1_workloads:
                    v2_workload = copy.deepcopy(v1_workload)
                    v2_workload["name"] = name
                    subtasks.append(
                        SubTask({"title": name, "workloads": [v2_workload]}))
            return subtasks


class SubTask(object):
    """Subtask -- unit of execution in Task

    """
    def __init__(self, config):
        """Subtask constructor.

        :param config: Dict with configuration of specified subtask
        """
        self.title = config["title"]
        self.tags = config.get("tags", [])
        self.group = config.get("group")
        self.description = config.get("description")
        self.workloads = [Workload(wconf)
                          for wconf
                          in config["workloads"]]
        self.context = config.get("context", {})

    def to_dict(self):
        return {
            "title": self.title,
            "description": self.description,
            "context": self.context,
        }


class Workload(object):
    """Workload -- workload configuration in SubTask.

    """
    def __init__(self, config):
        self.name = config["name"]
        self.runner = config.get("runner", {})
        self.sla = config.get("sla", {})
        self.hooks = config.get("hooks", [])
        self.context = config.get("context", {})
        self.args = config.get("args", {})

    def to_dict(self):
        workload = {"runner": self.runner}

        for prop in "sla", "args", "context", "hooks":
            value = getattr(self, prop)
            if value:
                workload[prop] = value

        return workload

    def to_task(self):
        """Make task configuration for the workload.

        This method returns a dict representing full configuration
        of the task containing a single subtask with this single
        workload.

        :return: dict containing full task configuration
        """
        # NOTE(ikhudoshyn): Result of this method will be used
        # to store full task configuration in DB so that
        # subtask configuration in reports would be given
        # in the same format as it was provided by user.
        # Temporarily it returns to_dict() in order not
        # to break existing reports. It should be
        # properly implemented in a patch that will update reports.
        # return {self.name: [self.to_dict()]}
        return self.to_dict()

    def make_key(self, pos):
        return {"name": self.name,
                "pos": pos,
                "kw": self.to_task()}

    def make_exception_args(self, pos, reason):
        return {"name": self.name,
                "pos": pos,
                "config": self.to_dict(),
                "reason": reason}
