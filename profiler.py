# Copyright (c) 2013 OpenStack Foundation.
# All rights reserved.
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

import abc

from neutron_lib.api import extensions as api_extensions
from neutron_lib import constants
from neutron_lib import exceptions
from neutron_lib.plugins import directory
import six

from neutron._i18n import _
from neutron.api import extensions
from neutron.api.v2 import base
from neutron.api.v2 import resource
from neutron.common import rpc as n_rpc
from neutron.extensions import agent
from neutron import policy
from neutron import wsgi
from neutron.services import service_base

PROFILER = 'profiler'
PROFILERS = PROFILER + 's'
PROFILER_PLUGIN_TYPE = 'PROFILER'


class ProfilerController(wsgi.Controller):
    def index(self, request, **kwargs):
        print("in ProfilerController index")
        import traceback
        traceback.print_stack()
        plugin = directory.get_plugin()
        return {}
        #policy.enforce(request.context,
        #               "get_%s" % PROFILERS,
        #               {})
        #return plugin.list_networks_on_dhcp_agent(
        #    request.context, kwargs['agent_id'])

    def create(self, request, body, **kwargs):
        plugin = directory.get_plugin()
        policy.enforce(request.context,
                       "create_%s" % PROFILER,
                       {})
        agent_id = kwargs['agent_id']
        network_id = body['network_id']
        result = {}
        #result = plugin.add_network_to_dhcp_agent(request.context, agent_id,
        #                                          network_id)
        #notify(request.context, 'dhcp_agent.network.add', network_id, agent_id)
        return result

    def delete(self, request, id, **kwargs):
        plugin = directory.get_plugin()
        policy.enforce(request.context,
                       "delete_%s" % PROFILER,
                       {})
        result = {}
        agent_id = kwargs['agent_id']
        #result = plugin.remove_network_from_dhcp_agent(request.context,
        #                                               agent_id, id)
        #notify(request.context, 'dhcp_agent.network.remove', id, agent_id)
        return result


class Profiler(api_extensions.ExtensionDescriptor):
    """Extension class supporting profiler.
    """

    @classmethod
    def get_name(cls):
        return "Profiler"

    @classmethod
    def get_alias(cls):
        return "profiler"

    @classmethod
    def get_description(cls):
        return "profiler"

    @classmethod
    def get_updated(cls):
        return "2017-01-01T10:00:00-00:00"

    @classmethod
    def get_resources(cls):
        """Returns Ext Resources."""
        exts = []
        parent = dict(member_name="agent",
                      collection_name="agents")
        controller = resource.Resource(ProfilerController(),
                                       base.FAULT_MAP)
        exts.append(extensions.ResourceExtension(
            PROFILERS, controller))

        return exts

    def get_extended_resources(self, version):
        return {}



def notify(context, action, network_id, agent_id):
    info = {'id': agent_id, 'network_id': network_id}
    notifier = n_rpc.get_notifier('network')
    notifier.info(context, action, {'agent': info})


class ProfilerPushToServersRpcApi(object):
    """Publisher-side RPC (stub) for plugin-to-plugin fanout interaction.

    This class implements the client side of an rpc interface.  The receiver
    side can be found below: ResourcesPushToServerRpcCallback.  For more
    information on this RPC interface, see doc/source/devref/rpc_callbacks.rst.
    """

    def __init__(self):
        target = oslo_messaging.Target(
            topic=topics.PROFILER, version='1.0',
            namespace=constants.RPC_NAMESPACE_PROFILER)
        self.client = n_rpc.get_client(target)

    @log_helpers.log_method_call
    def profiler_action(self, context, task_id, action);
        """Fan out all the agent resource versions to other servers."""
        print("plugin-to-plugin, Client-side RPC")
        cctxt = self.client.prepare(fanout=True)
        cctxt.cast(context, 'profiler_action',
                   task_id=task_id,
                   action=action)


class ProfilerPushToServerRpcCallback(object):
    """Receiver-side RPC (implementation) for plugin-to-plugin interaction.

    This class implements the receiver side of an rpc interface.
    The client side can be found above: ResourcePushToServerRpcApi.  For more
    information on this RPC interface, see doc/source/devref/rpc_callbacks.rst.
    """

    # History
    #   1.0 Initial version

    target = oslo_messaging.Target(
        version='1.0', namespace=constants.RPC_NAMESPACE_PROFILER)

    @log_helpers.log_method_call
    def profiler_action(self, context, task_id, action):
        print("plugin-to-plugin, Receiver-side RPC")


