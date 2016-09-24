#!/usr/bin/env python
#
# Copyright 2016 the original author or authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""Virtual OLT Hardware Abstraction main entry point"""

import argparse
import os
import time

import yaml
from twisted.internet.defer import inlineCallbacks

from voltha.coordinator import Coordinator
from voltha.dockerhelpers import get_my_containers_name
from voltha.nethelpers import get_my_primary_interface, get_my_primary_local_ipv4
from voltha.northbound.grpc.grpc_server import VolthaGrpcServer
from voltha.northbound.rest.health_check import init_rest_service
from voltha.structlog_setup import setup_logging

defs = dict(
    consul=os.environ.get('CONSUL', 'localhost:8500'),
    instance_id=os.environ.get('INSTANCE_ID', os.environ.get('HOSTNAME', '1')),
    config=os.environ.get('CONFIG', './voltha.yml'),
    interface=os.environ.get('INTERFACE', get_my_primary_interface()),
    internal_host_address=os.environ.get('INTERNAL_HOST_ADDRESS',
                                         get_my_primary_local_ipv4()),
    external_host_address=os.environ.get('EXTERNAL_HOST_ADDRESS',
                                         get_my_primary_local_ipv4()),
    fluentd=os.environ.get('FLUENTD', None),
    rest_port=os.environ.get('REST_PORT', 8880),
)


def parse_args():

    parser = argparse.ArgumentParser()

    _help = ('Path to voltha.yml config file (default: %s). '
             'If relative, it is relative to main.py of voltha.'
             % defs['config'])
    parser.add_argument('-c', '--config',
                        dest='config',
                        action='store',
                        default=defs['config'],
                        help=_help)

    _help = '<hostname>:<port> to consul agent (default: %s)' % defs['consul']
    parser.add_argument(
        '-C', '--consul', dest='consul', action='store',
        default=defs['consul'],
        help=_help)

    _help = ('<hostname> or <ip> at which Voltha is reachable from outside '
             'the cluster (default: %s)' % defs['external_host_address'])
    parser.add_argument('-E', '--external-host-address',
                        dest='external_host_address',
                        action='store',
                        default=defs['external_host_address'],
                        help=_help)

    _help = ('<hostname>:<port> to fluentd server (default: %s). (If not '
             'specified (None), the address from the config file is used'
             % defs['fluentd'])
    parser.add_argument('-F', '--fluentd',
                        dest='fluentd',
                        action='store',
                        default=defs['fluentd'],
                        help=_help)

    _help = ('<hostname> or <ip> at which Voltha is reachable from inside the'
             'cluster (default: %s)' % defs['internal_host_address'])
    parser.add_argument('-H', '--internal-host-address',
                        dest='internal_host_address',
                        action='store',
                        default=defs['internal_host_address'],
                        help=_help)

    _help = ('unique string id of this voltha instance (default: %s)'
             % defs['interface'])
    parser.add_argument('-i', '--instance-id',
                        dest='instance_id',
                        action='store',
                        default=defs['instance_id'],
                        help=_help)

    # TODO placeholder, not used yet
    _help = 'ETH interface to send (default: %s)' % defs['interface']
    parser.add_argument('-I', '--interface',
                        dest='interface',
                        action='store',
                        default=defs['interface'],
                        help=_help)

    _help = 'omit startup banner log lines'
    parser.add_argument('-n', '--no-banner',
                        dest='no_banner',
                        action='store_true',
                        default=False,
                        help=_help)

    _help = 'do not emit periodic heartbeat log messages'
    parser.add_argument('-N', '--no-heartbeat',
                        dest='no_heartbeat',
                        action='store_true',
                        default=False,
                        help=_help)

    _help = ('port number for the rest service (default: %d)'
             % defs['rest_port'])
    parser.add_argument('-R', '--rest-port',
                        dest='rest_port',
                        action='store',
                        type=int,
                        default=defs['rest_port'],
                        help=_help)

    _help = "suppress debug and info logs"
    parser.add_argument('-q', '--quiet',
                        dest='quiet',
                        action='count',
                        help=_help)

    _help = 'enable verbose logging'
    parser.add_argument('-v', '--verbose',
                        dest='verbose',
                        action='count',
                        help=_help)

    _help = ('use docker container name as voltha instance id'
             ' (overrides -i/--instance-id option)')
    parser.add_argument('--instance-id-is-container-name',
                        dest='instance_id_is_container_name',
                        action='store_true',
                        default=False,
                        help=_help)

    args = parser.parse_args()

    # post-processing

    if args.instance_id_is_container_name:
        args.instance_id = get_my_containers_name()

    return args


def load_config(args):
    path = args.config
    if path.startswith('.'):
        dir = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(dir, path)
    path = os.path.abspath(path)
    with open(path) as fd:
        config = yaml.load(fd)
    return config


def print_banner(log):
    log.info(' _    ______  __  ________  _____ ')
    log.info('| |  / / __ \/ / /_  __/ / / /   |')
    log.info('| | / / / / / /   / / / /_/ / /| |')
    log.info('| |/ / /_/ / /___/ / / __  / ___ |')
    log.info('|___/\____/_____/_/ /_/ /_/_/  |_|')
    log.info('(to stop: press Ctrl-C)')


class Main(object):

    def __init__(self):

        self.args = args = parse_args()
        self.config = load_config(args)

        verbosity_adjust = (args.verbose or 0) - (args.quiet or 0)
        self.log = setup_logging(self.config.get('logging', {}),
                                 args.instance_id,
                                 verbosity_adjust=verbosity_adjust,
                                 fluentd=args.fluentd)

        # components
        self.coordinator = None
        self.grpc_server = None

        if not args.no_banner:
            print_banner(self.log)

        if not args.no_heartbeat:
            self.start_heartbeat()

        self.startup_components()

    def start(self):
        self.start_reactor()  # will not return except Keyboard interrupt

    def startup_components(self):
        self.log.info('starting-internal-components')
        self.coordinator = Coordinator(
            internal_host_address=self.args.internal_host_address,
            external_host_address=self.args.external_host_address,
            rest_port=self.args.rest_port,
            instance_id=self.args.instance_id,
            consul=self.args.consul)
        init_rest_service(self.args.rest_port)

        self.grpc_server = VolthaGrpcServer().run()

        self.log.info('started-internal-services')

    @inlineCallbacks
    def shutdown_components(self):
        """Execute before the reactor is shut down"""
        self.log.info('exiting-on-keyboard-interrupt')
        if self.coordinator is not None:
            yield self.coordinator.shutdown()
        if self.grpc_server is not None:
            yield self.grpc_server.shutdown()

    def start_reactor(self):
        from twisted.internet import reactor
        reactor.callWhenRunning(
            lambda: self.log.info('twisted-reactor-started'))
        reactor.addSystemEventTrigger('before', 'shutdown',
                                      self.shutdown_components)
        reactor.run()

    def start_heartbeat(self):

        t0 = time.time()
        t0s = time.ctime(t0)

        def heartbeat():
            self.log.debug(status='up', since=t0s, uptime=time.time() - t0)

        from twisted.internet.task import LoopingCall
        lc = LoopingCall(heartbeat)
        lc.start(10)


if __name__ == '__main__':
    Main().start()