# Core modules
import os
import re
import logging
import subprocess
import sys
import time
import datetime
import socket

import modules

from util import get_os, get_uuid, md5, Timer, get_hostname, EC2, GCE
from config import get_version, get_system_stats

import checks.system.unix as u
import checks.system.win32 as w32
from checks import create_service_check, AgentCheck
from checks.agent_metrics import CollectorMetrics
from checks.ganglia import Ganglia
from checks.datadog import Dogstreams, DdForwarder
from checks.check_status import CheckStatus, CollectorStatus, EmitterStatus, STATUS_OK, STATUS_ERROR
from resources.processes import Processes as ResProcesses


log = logging.getLogger(__name__)


FLUSH_LOGGING_PERIOD = 10
FLUSH_LOGGING_INITIAL = 5

class Collector(object):
    """
    The collector is responsible for collecting data from each check and
    passing it along to the emitters, who send it to their final destination.
    """

    def __init__(self, agentConfig, emitters, systemStats, hostname):
        self.emit_duration = None
        self.agentConfig = agentConfig
        self.hostname = hostname
        # system stats is generated by config.get_system_stats
        self.agentConfig['system_stats'] = systemStats
        # agent config is used during checks, system_stats can be accessed through the config
        self.os = get_os()
        self.plugins = None
        self.emitters = emitters
        self.check_timings = agentConfig.get('check_timings')
        self.push_times = {
            'metadata': {
                'start': time.time(),
                'interval': int(agentConfig.get('metadata_interval', 4 * 60 * 60))
            },
            'external_host_tags': {
                'start': time.time() - 3 * 60, # Wait for the checks to init
                'interval': int(agentConfig.get('external_host_tags', 5 * 60))
            },
            'agent_checks': {
                'start': time.time(),
                'interval': int(agentConfig.get('agent_checks_interval', 10 * 60))
            }
        }
        socket.setdefaulttimeout(15)
        self.run_count = 0
        self.continue_running = True
        self.metadata_cache = None
        self.initialized_checks_d = []
        self.init_failed_checks_d = {}

        # Unix System Checks
        self._unix_system_checks = {
            'disk': u.Disk(log),
            'io': u.IO(log),
            'load': u.Load(log),
            'memory': u.Memory(log),
            'processes': u.Processes(log),
            'cpu': u.Cpu(log)
        }

        # Win32 System `Checks
        self._win32_system_checks = {
            'disk': w32.Disk(log),
            'io': w32.IO(log),
            'proc': w32.Processes(log),
            'memory': w32.Memory(log),
            'network': w32.Network(log),
            'cpu': w32.Cpu(log)
        }

        # Old-style metric checks
        self._ganglia = Ganglia(log)
        self._dogstream = Dogstreams.init(log, self.agentConfig)
        self._ddforwarder = DdForwarder(log, self.agentConfig)

        # Agent Metrics
        self._agent_metrics = CollectorMetrics(log)

        self._metrics_checks = []

        # Custom metric checks
        for module_spec in [s.strip() for s in self.agentConfig.get('custom_checks', '').split(',')]:
            if len(module_spec) == 0: continue
            try:
                self._metrics_checks.append(modules.load(module_spec, 'Check')(log))
                log.info("Registered custom check %s" % module_spec)
                log.warning("Old format custom checks are deprecated. They should be moved to the checks.d interface as old custom checks will be removed in a next version")
            except Exception, e:
                log.exception('Unable to load custom check module %s' % module_spec)

        # Resource Checks
        self._resources_checks = [
            ResProcesses(log,self.agentConfig)
        ]

    def stop(self):
        """
        Tell the collector to stop at the next logical point.
        """
        # This is called when the process is being killed, so
        # try to stop the collector as soon as possible.
        # Most importantly, don't try to submit to the emitters
        # because the forwarder is quite possibly already killed
        # in which case we'll get a misleading error in the logs.
        # Best to not even try.
        self.continue_running = False
        for check in self.initialized_checks_d:
            check.stop()

    def run(self, checksd=None, start_event=True):
        """
        Collect data from each check and submit their data.
        """
        timer = Timer()
        if self.os != 'windows':
            cpu_clock = time.clock()
        self.run_count += 1
        log.debug("Starting collection run #%s" % self.run_count)

        payload = self._build_payload(start_event=start_event)
        metrics = payload['metrics']
        events = payload['events']
        service_checks = payload['service_checks']
        if checksd:
            self.initialized_checks_d = checksd['initialized_checks'] # is of type {check_name: check}
            self.init_failed_checks_d = checksd['init_failed_checks'] # is of type {check_name: {error, traceback}}
        # Run the system checks. Checks will depend on the OS
        if self.os == 'windows':
            # Win32 system checks
            try:
                metrics.extend(self._win32_system_checks['disk'].check(self.agentConfig))
                metrics.extend(self._win32_system_checks['memory'].check(self.agentConfig))
                metrics.extend(self._win32_system_checks['cpu'].check(self.agentConfig))
                metrics.extend(self._win32_system_checks['network'].check(self.agentConfig))
                metrics.extend(self._win32_system_checks['io'].check(self.agentConfig))
                metrics.extend(self._win32_system_checks['proc'].check(self.agentConfig))
            except Exception:
                log.exception('Unable to fetch Windows system metrics.')
        else:
            # Unix system checks
            sys_checks = self._unix_system_checks

            diskUsage = sys_checks['disk'].check(self.agentConfig)
            if diskUsage and len(diskUsage) == 2:
                payload["diskUsage"] = diskUsage[0]
                payload["inodes"] = diskUsage[1]

            load = sys_checks['load'].check(self.agentConfig)
            payload.update(load)

            memory = sys_checks['memory'].check(self.agentConfig)

            if memory:
                payload.update({
                    'memPhysUsed' : memory.get('physUsed'),
                    'memPhysPctUsable' : memory.get('physPctUsable'),
                    'memPhysFree' : memory.get('physFree'),
                    'memPhysTotal' : memory.get('physTotal'),
                    'memPhysUsable' : memory.get('physUsable'),
                    'memSwapUsed' : memory.get('swapUsed'),
                    'memSwapFree' : memory.get('swapFree'),
                    'memSwapPctFree' : memory.get('swapPctFree'),
                    'memSwapTotal' : memory.get('swapTotal'),
                    'memCached' : memory.get('physCached'),
                    'memBuffers': memory.get('physBuffers'),
                    'memShared': memory.get('physShared')
                })

            ioStats = sys_checks['io'].check(self.agentConfig)
            if ioStats:
                payload['ioStats'] = ioStats

            processes = sys_checks['processes'].check(self.agentConfig)
            payload.update({'processes': processes})

            cpuStats = sys_checks['cpu'].check(self.agentConfig)
            if cpuStats:
                payload.update(cpuStats)

        # Run old-style checks
        gangliaData = self._ganglia.check(self.agentConfig)
        dogstreamData = self._dogstream.check(self.agentConfig)
        ddforwarderData = self._ddforwarder.check(self.agentConfig)


        if gangliaData is not False and gangliaData is not None:
            payload['ganglia'] = gangliaData

        # dogstream
        if dogstreamData:
            dogstreamEvents = dogstreamData.get('dogstreamEvents', None)
            if dogstreamEvents:
                if 'dogstream' in payload['events']:
                    events['dogstream'].extend(dogstreamEvents)
                else:
                    events['dogstream'] = dogstreamEvents
                del dogstreamData['dogstreamEvents']

            payload.update(dogstreamData)

        # metrics about the forwarder
        if ddforwarderData:
            payload['datadog'] = ddforwarderData

        # Resources checks
        if self.os != 'windows':
            has_resource = False
            for resources_check in self._resources_checks:
                resources_check.check()
                snaps = resources_check.pop_snapshots()
                if snaps:
                    has_resource = True
                    res_value = { 'snaps': snaps,
                                  'format_version': resources_check.get_format_version() }
                    res_format = resources_check.describe_format_if_needed()
                    if res_format is not None:
                        res_value['format_description'] = res_format
                    payload['resources'][resources_check.RESOURCE_KEY] = res_value

            if has_resource:
                payload['resources']['meta'] = {
                            'api_key': self.agentConfig['api_key'],
                            'host': payload['internalHostname'],
                        }

        # newer-style checks (not checks.d style)
        for metrics_check in self._metrics_checks:
            res = metrics_check.check(self.agentConfig)
            if res:
                metrics.extend(res)

        # checks.d checks
        check_statuses = []
        for check in self.initialized_checks_d:
            if not self.continue_running:
                return
            log.info("Running check %s" % check.name)
            instance_statuses = []
            metric_count = 0
            event_count = 0
            service_check_count = 0
            check_start_time = time.time()
            try:
                # Run the check.
                instance_statuses = check.run()

                # Collect the metrics and events.
                current_check_metrics = check.get_metrics()
                current_check_events = check.get_events()

                # Collect metadata
                current_check_metadata = check.get_service_metadata()

                # Save metrics & events for the payload.
                metrics.extend(current_check_metrics)
                if current_check_events:
                    if check.name not in events:
                        events[check.name] = current_check_events
                    else:
                        events[check.name] += current_check_events

                # Save the status of the check.
                metric_count = len(current_check_metrics)
                event_count = len(current_check_events)
            except Exception:
                log.exception("Error running check %s" % check.name)

            check_status = CheckStatus(check.name, instance_statuses, metric_count, event_count, service_check_count,
                library_versions=check.get_library_info(),
                source_type_name=check.SOURCE_TYPE_NAME or check.name,
                service_metadata=current_check_metadata)

            # Service check for Agent checks failures
            service_check_tags = ["check:%s" % check.name]
            if check_status.status == STATUS_OK:
                status = AgentCheck.OK
            elif check_status.status == STATUS_ERROR:
                status = AgentCheck.CRITICAL
            check.service_check('datadog.agent.check_status', status, tags=service_check_tags)

            # Collect the service checks and save them in the payload
            current_check_service_checks = check.get_service_checks()
            if current_check_service_checks:
                service_checks.extend(current_check_service_checks)
            service_check_count = len(current_check_service_checks)

            # Update the check status with the correct service_check_count
            check_status.service_check_count = service_check_count
            check_statuses.append(check_status)

            check_run_time = time.time() - check_start_time
            log.debug("Check %s ran in %.2f s" % (check.name, check_run_time))

            # Intrument check run timings if enabled.
            if self.check_timings:
                metric = 'datadog.agent.check_run_time'
                meta = {'tags': ["check:%s" % check.name]}
                metrics.append((metric, time.time(), check_run_time, meta))

        for check_name, info in self.init_failed_checks_d.iteritems():
            if not self.continue_running:
                return
            check_status = CheckStatus(check_name, None, None, None, None,
                                       init_failed_error=info['error'],
                                       init_failed_traceback=info['traceback'])
            check_statuses.append(check_status)

        # Add a service check for the agent
        service_checks.append(create_service_check('datadog.agent.up', AgentCheck.OK,
            hostname=self.hostname))

        # Store the metrics and events in the payload.
        payload['metrics'] = metrics
        payload['events'] = events
        payload['service_checks'] = service_checks

        if self._should_send_additional_data('agent_checks'):
            # Add agent checks statuses and error/warning messages
            agent_checks = []
            for check in check_statuses:
                if check.instance_statuses is not None:
                    instance_index = 0
                    for instance_status in check.instance_statuses:
                        # Attach metadata dictionnaries to their instances
                        if(check.service_metadata and instance_status.status==STATUS_OK):
                            # Only correctly running services have metadata
                            instance_meta = check.service_metadata[instance_index]
                            instance_index += 1
                        else:
                            instance_meta = {}

                        agent_checks.append(
                            (
                                check.name, check.source_type_name,
                                instance_status.instance_id,
                                instance_status.status,
                                # put error message or list of warning messages in the same field
                                # it will be handled by the UI
                                instance_status.error or instance_status.warnings or "",
                                instance_meta
                            )
                        )
                else:
                    agent_checks.append(
                        (
                            check.name, check.source_type_name,
                            "initialization",
                            check.status, repr(check.init_failed_error)
                        )
                    )
            payload['agent_checks'] = agent_checks
            payload['meta'] = self.metadata_cache  # add hostname metadata
        collect_duration = timer.step()

        if self.os != 'windows':
            payload['metrics'].extend(self._agent_metrics.check(payload, self.agentConfig,
                collect_duration, self.emit_duration, time.clock() - cpu_clock))
        else:
            payload['metrics'].extend(self._agent_metrics.check(payload, self.agentConfig,
                collect_duration, self.emit_duration))


        emitter_statuses = self._emit(payload)
        self.emit_duration = timer.step()

        # Persist the status of the collection run.
        try:
            CollectorStatus(check_statuses, emitter_statuses, self.metadata_cache).persist()
        except Exception:
            log.exception("Error persisting collector status")

        if self.run_count <= FLUSH_LOGGING_INITIAL or self.run_count % FLUSH_LOGGING_PERIOD == 0:
            log.info("Finished run #%s. Collection time: %ss. Emit time: %ss" %
                    (self.run_count, round(collect_duration, 2), round(self.emit_duration, 2)))
            if self.run_count == FLUSH_LOGGING_INITIAL:
                log.info("First flushes done, next flushes will be logged every %s flushes." % FLUSH_LOGGING_PERIOD)

        else:
            log.debug("Finished run #%s. Collection time: %ss. Emit time: %ss" %
                    (self.run_count, round(collect_duration, 2), round(self.emit_duration, 2)))

        return payload

    def _emit(self, payload):
        """ Send the payload via the emitters. """
        statuses = []
        for emitter in self.emitters:
            # Don't try to send to an emitter if we're stopping/
            if not self.continue_running:
                return statuses
            name = emitter.__name__
            emitter_status = EmitterStatus(name)
            try:
                emitter(payload, log, self.agentConfig)
            except Exception, e:
                log.exception("Error running emitter: %s" % emitter.__name__)
                emitter_status = EmitterStatus(name, e)
            statuses.append(emitter_status)
        return statuses

    def _is_first_run(self):
        return self.run_count <= 1

    def _build_payload(self, start_event=True):
        """
        Return an dictionary that contains all of the generic payload data.
        """
        now = time.time()
        payload = {
            'collection_timestamp': now,
            'os' : self.os,
            'python': sys.version,
            'agentVersion' : self.agentConfig['version'],
            'apiKey': self.agentConfig['api_key'],
            'events': {},
            'metrics': [],
            'service_checks': [],
            'resources': {},
            'internalHostname' : self.hostname,
            'uuid' : get_uuid(),
            'host-tags': {},
            'external_host_tags': {}
        }

        # Include system stats on first postback
        if start_event and self._is_first_run():
            payload['systemStats'] = self.agentConfig.get('system_stats', {})
            # Also post an event in the newsfeed
            payload['events']['System'] = [{'api_key': self.agentConfig['api_key'],
                                 'host': payload['internalHostname'],
                                 'timestamp': now,
                                 'event_type':'Agent Startup',
                                 'msg_text': 'Version %s' % get_version()
                                 }]

        # Periodically send the host metadata.
        if self._should_send_additional_data('metadata'):
            # gather metadata with gohai
            try:
                if get_os() != 'windows':
                    command = "gohai"
                else:
                    command = "gohai\gohai.exe"
                gohai_metadata = subprocess.Popen(
                    [command], stdout=subprocess.PIPE
                ).communicate()[0]
                payload['gohai'] = gohai_metadata
            except OSError as e:
                if e.errno == 2:  # file not found, expected when install from source
                    log.info("gohai file not found")
                else:
                    raise e
            except Exception as e:
                log.warning("gohai command failed with error %s" % str(e))

            payload['systemStats'] = get_system_stats()
            payload['meta'] = self._get_metadata()

            self.metadata_cache = payload['meta']
            # Add static tags from the configuration file
            host_tags = []
            if self.agentConfig['tags'] is not None:
                host_tags.extend([unicode(tag.strip()) for tag in self.agentConfig['tags'].split(",")])

            if self.agentConfig['collect_ec2_tags']:
                host_tags.extend(EC2.get_tags(self.agentConfig))

            if host_tags:
                payload['host-tags']['system'] = host_tags

            GCE_tags = GCE.get_tags(self.agentConfig)
            if GCE_tags is not None:
                payload['host-tags'][GCE.SOURCE_TYPE_NAME] = GCE_tags

            # Log the metadata on the first run
            if self._is_first_run():
                log.info("Hostnames: %s, tags: %s" % (repr(self.metadata_cache), payload['host-tags']))

        # Periodically send extra hosts metadata (vsphere)
        # Metadata of hosts that are not the host where the agent runs, not all the checks use
        # that
        external_host_tags = []
        if self._should_send_additional_data('external_host_tags'):
            for check in self.initialized_checks_d:
                try:
                    getter = getattr(check, 'get_external_host_tags')
                    check_tags = getter()
                    external_host_tags.extend(check_tags)
                except AttributeError:
                    pass

        if external_host_tags:
            payload['external_host_tags'] = external_host_tags

        return payload

    def _get_metadata(self):
        metadata = EC2.get_metadata(self.agentConfig)
        if metadata.get('hostname'):
            metadata['ec2-hostname'] = metadata.get('hostname')
            del metadata['hostname']

        if self.agentConfig.get('hostname'):
            metadata['agent-hostname'] = self.agentConfig.get('hostname')
        else:
            try:
                metadata["socket-hostname"] = socket.gethostname()
            except Exception:
                pass
        try:
            metadata["socket-fqdn"] = socket.getfqdn()
        except Exception:
            pass

        metadata["hostname"] = get_hostname()

        return metadata

    def _should_send_additional_data(self, data_name):
        if self._is_first_run():
            return True
        # If the interval has passed, send the metadata again
        now = time.time()
        if now - self.push_times[data_name]['start'] >= self.push_times[data_name]['interval']:
            log.debug('%s interval has passed. Sending it.' % data_name)
            self.push_times[data_name]['start'] = now
            return True

        return False


