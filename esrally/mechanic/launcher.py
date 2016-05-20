import os
import threading
import subprocess
import socket
import signal
import logging
import json

from esrally.mechanic import gear
from esrally import config, cluster, telemetry, time, exceptions
from esrally.track import track

logger = logging.getLogger("rally.launcher")


class ClusterFactory:
    def create(self, hosts, nodes, metrics_store, telemetry):
        return cluster.Cluster(hosts, nodes, metrics_store, telemetry)


class Launcher:
    def __init__(self, cfg, cluster_factory_class=ClusterFactory):
        self.cfg = cfg
        self.cluster_factory = cluster_factory_class()

    def setup_index(self, cluster, t, challenge):
        if track.BenchmarkPhase.index in challenge.benchmark:
            mapping_path = self.cfg.opts("benchmarks", "mapping.path")
            settings = challenge.benchmark[track.BenchmarkPhase.index].index_settings
            # Workaround to support multiple versions (this is not how this will be handled in the future..)
            if "master" in settings:
                # check whether we do a binary benchmark
                distribution_version = self.cfg.opts("source", "distribution.version", mandatory=False)
                if distribution_version and len(distribution_version.strip()) > 0:
                    if distribution_version in settings:
                        index_settings = settings[distribution_version]
                    else:
                        raise exceptions.SystemSetupError("Could not find index settings for Elasticsearch version [%s]" %
                                                          distribution_version)
                else:
                    index_settings = settings["master"]
            else:
                index_settings = settings
            for index in t.indices:
                logger.debug("Creating index [%s]" % index.name)
                cluster.client.indices.create(index=index.name, body=index_settings)
                for type in index.types:
                    mappings = open(mapping_path[type]).read()
                    logger.debug("create mapping for type [%s] in index [%s]" % (type.name, index.name))
                    logger.debug(mappings)
                    cluster.client.indices.put_mapping(index=index.name,
                                                       doc_type=type.name,
                                                       body=json.loads(mappings))
        cluster.wait_for_status_green()


class ExternalLauncher(Launcher):
    def __init__(self, cfg, cluster_factory_class=ClusterFactory):
        super().__init__(cfg, cluster_factory_class)

    def start(self, track, challenge, metrics_store):
        configured_host_list = self.cfg.opts("launcher", "external.target.hosts")
        hosts = []
        try:
            for authority in configured_host_list:
                host, port = authority.split(":")
                hosts.append({"host": host, "port": port})
        except ValueError:
            msg = "Could not initialize external cluster. Invalid format for %s. Expected a comma-separated list of host:port pairs, " \
                  "e.g. host1:9200,host2:9200." % configured_host_list
            logger.exception(msg)
            raise exceptions.SystemSetupError(msg)

        t = telemetry.Telemetry(self.cfg, metrics_store, devices=[
            telemetry.ExternalEnvironmentInfo(self.cfg, metrics_store),
            telemetry.NodeStats(self.cfg, metrics_store),
            telemetry.IndexStats(self.cfg, metrics_store)
        ])
        c = self.cluster_factory.create(hosts, [], metrics_store, t)
        t.attach_to_cluster(c)
        self.setup_index(c, track, challenge)
        return c


class InProcessLauncher(Launcher):
    """
    Launcher is responsible for starting and stopping the benchmark candidate.

    Currently, only local launching is supported.
    """
    PROCESS_WAIT_TIMEOUT_SECONDS = 20.0

    # TODO 68: We should externalize this (see #68)
    ES_CMD_LINE_OPTS_PER_VERSION = {
        "5.0.0-alpha1": {
            "processors": "-Ees.processors",
            "log_path": "-Ees.path.logs",
            "node_name": "-Ees.node.name"
        },
        "5.0.0-alpha2": {
            "processors": "-Ees.processors",
            "log_path": "-Ees.path.logs",
            "node_name": "-Ees.node.name"
        },
        "master": {
            "processors": "-Eprocessors",
            "log_path": "-Epath.logs",
            "node_name": "-Enode.name"
        }
    }

    def __init__(self, cfg, clock=time.Clock, cluster_factory_class=ClusterFactory):
        super().__init__(cfg, cluster_factory_class)
        self._clock = clock
        self._servers = []

    def start(self, track, challenge, car, metrics_store):
        if self._servers:
            logger.warn("There are still referenced servers on startup. Did the previous shutdown succeed?")
        first_http_port = self.cfg.opts("provisioning", "node.http.port")

        t = telemetry.Telemetry(self.cfg, metrics_store)
        c = self.cluster_factory.create(
            [{"host": "localhost", "port": first_http_port}],
            [self._start_node(node, car, metrics_store) for node in range(car.nodes)],
            metrics_store, t
        )
        t.attach_to_cluster(c)
        self.setup_index(c, track, challenge)
        return c

    def _start_node(self, node, car, metrics_store):
        node_name = self._node_name(node)
        host_name = socket.gethostname()
        t = telemetry.Telemetry(self.cfg, metrics_store)

        env = self._prepare_env(car, node_name, t)
        cmd = self.prepare_cmd(car, node_name)
        process = self._start_process(cmd, env, node_name)
        node = cluster.Node(process, host_name, node_name, t)
        t.attach_to_node(node)

        return node

    def _prepare_env(self, car, node_name, t):
        env = {}
        env.update(os.environ)
        # we just blindly trust telemetry here...
        for k, v in t.instrument_candidate_env(car, node_name).items():
            self._set_env(env, k, v)

        java_opts = ""
        if car.heap:
            java_opts += "-Xms%s -Xmx%s " % (car.heap, car.heap)
        if car.java_opts:
            java_opts += car.java_opts
        if len(java_opts) > 0:
            self._set_env(env, "ES_JAVA_OPTS", java_opts)
        self._set_env(env, "ES_GC_OPTS", car.gc_opts)

        java_home = gear.Gear(self.cfg).capability(gear.Capability.java)
        # Unix specific!:
        self._set_env(env, "PATH", "%s/bin" % java_home, separator=":")
        # Don't merge here!
        env["JAVA_HOME"] = java_home
        logger.info("ENV: %s" % str(env))
        return env

    def _set_env(self, env, k, v, separator=' '):
        if v is not None:
            if k not in env:
                env[k] = v
            else:  # merge
                env[k] = v + separator + env[k]

    def prepare_cmd(self, car, node_name):
        server_log_dir = "%s/server" % self.cfg.opts("system", "challenge.log.dir")
        self.cfg.add(config.Scope.invocation, "launcher", "candidate.log.dir", server_log_dir)
        distribution_version = self.cfg.opts("source", "distribution.version", mandatory=False)

        cmd = ["bin/elasticsearch",
               "%s=%s" % (self.cmd_line_opt(distribution_version, "node_name"), node_name),
               "%s=%s" % (self.cmd_line_opt(distribution_version, "log_path"), server_log_dir)
               ]
        processor_count = car.processors
        if processor_count is not None and processor_count > 1:
            cmd.append("%s=%s" % (self.cmd_line_opt(distribution_version, "processors"), processor_count))
        logger.info("ES launch: %s" % str(cmd))
        return cmd

    def cmd_line_opt(self, distribution_version, key):
        if distribution_version and len(distribution_version.strip()) > 0:
            if distribution_version in InProcessLauncher.ES_CMD_LINE_OPTS_PER_VERSION:
                return InProcessLauncher.ES_CMD_LINE_OPTS_PER_VERSION[distribution_version][key]
        # assume master, it will fail anyway otherwise and we can add the version then
        return InProcessLauncher.ES_CMD_LINE_OPTS_PER_VERSION["master"][key]

    def _start_process(self, cmd, env, node_name):
        install_dir = self.cfg.opts("provisioning", "local.binary.path")
        os.chdir(install_dir)
        startup_event = threading.Event()
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env)
        t = threading.Thread(target=self._read_output, args=(node_name, process, startup_event))
        t.setDaemon(True)
        t.start()
        if startup_event.wait(timeout=InProcessLauncher.PROCESS_WAIT_TIMEOUT_SECONDS):
            logger.info("Started node=%s with pid=%s" % (node_name, process.pid))
            return process
        else:
            log_dir = self.cfg.opts("system", "log.dir")
            msg = "Could not start node '%s' within timeout period of %s seconds." % (
                node_name, InProcessLauncher.PROCESS_WAIT_TIMEOUT_SECONDS)
            logger.error(msg)
            raise exceptions.LaunchError("%s Please check the logs in '%s' for more details." % (msg, log_dir))

    def _node_name(self, node):
        prefix = self.cfg.opts("provisioning", "node.name.prefix")
        return "%s%d" % (prefix, node)

    def _read_output(self, node_name, server, startup_event):
        """
        Reads the output from the ES (node) subprocess.
        """
        while True:
            l = server.stdout.readline().decode("utf-8")
            if len(l) == 0:
                break
            l = l.rstrip()

            if l.find("Initialization Failed") != -1:
                startup_event.set()

            logger.info("%s: %s" % (node_name, l.replace("\n", "\n%s (stdout): " % node_name)))
            if l.endswith("started") and not startup_event.isSet():
                startup_event.set()
                logger.info("%s: started" % node_name)

    def stop(self, cluster):
        logger.info("Shutting down ES cluster")

        # Ask all nodes to shutdown:
        stop_watch = self._clock.stop_watch()
        stop_watch.start()
        for node in cluster.nodes:
            process = node.process
            node.telemetry.detach_from_node(node)

            os.kill(process.pid, signal.SIGINT)

            try:
                process.wait(10.0)
                logger.info("Done shutdown node (%.1f sec)" % stop_watch.split_time())
            except subprocess.TimeoutExpired:
                # kill -9
                logger.warn("Server %s did not shut down itself after 10 seconds; now kill -QUIT node, to see threads:" % node.node_name)
                try:
                    os.kill(process.pid, signal.SIGQUIT)
                except OSError:
                    logger.warn("  no such process")
                    return

                try:
                    process.wait(120.0)
                    logger.info("Done shutdown node (%.1f sec)" % stop_watch.split_time())
                    return
                except subprocess.TimeoutExpired:
                    pass

                logger.info("kill -KILL node")
                try:
                    process.kill()
                except ProcessLookupError:
                    logger.warn("No such process")
        cluster.telemetry.detach_from_cluster(cluster)
        self._servers = []
