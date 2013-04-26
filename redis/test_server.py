import re
import tempfile
import os
import sys
import signal
import time
import logging
import subprocess

import redis
from redis import utils


REDIS_TESTSERVER_PORT = int(os.getenv("REDIS_TESTSERVER_PORT", 9999))
REDIS_TESTSERVER_ADDRESS = os.getenv("REDIS_TESTSERVER_ADDRESS", "127.0.0.1")
REDIS_TESTSERVER_REDIS_EXE = \
    os.getenv("REDIS_TESTSERVER_REDIS_EXE", "redis-server")
REDIS_TESTSERVER_STARTUP_DELAY_S = \
    int(os.getenv("REDIS_TESTSERVER_STARTUP_DELAY_S", 2))


logger = logging.getLogger(__name__)


class KeyPairMapping(dict):
    "support class to map text <-> dict"
    def __init__(self, text=None):
        if not text:
            text = self.template

        super(KeyPairMapping, self).__init__()
        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("#") or line == "":
                continue
            i = line.find(" ")
            if i == -1:
                key = line.strip()
                value = ""
            else:
                key = line[:i]
                value = line[i+1:].strip()

            if key in self:
                if isinstance(self[key], (list, tuple)):
                    self[key].append(value)
                else:
                    self[key] = [self[key], value]
            else:
                self[key] = value

    @staticmethod
    def to_str(data, eol='\n'):
        result = []
        for k in sorted(data.keys()):
            if k.startswith("__"):
                continue
            values = data[k]
            if not isinstance(data[k], (list, tuple)):
                values = [data[k]]
            for value in values:
                result.append(str(k) + " " + str(value))
        if eol:
            result = eol.join(result)
        return result


class ServerConfig(KeyPairMapping):
    template = """
# Skeleton template for config a redis server
daemonize       no
port            %s
bind            %s
timeout         0
tcp-keepalive   0
loglevel        notice
databases       16
""" % (REDIS_TESTSERVER_PORT, REDIS_TESTSERVER_ADDRESS)


class TestServerBase(object):
    """test server launcher class

    This class wraps a redis-server instance providing support for
    testing.

"""
    def __init__(self, keypairs=None):

        self.server_config = ServerConfig()
        self.server_config.update(keypairs if keypairs else {})

        self.config = {}
        self.config['redis'] = REDIS_TESTSERVER_REDIS_EXE
        self.config['startup_delay_s'] = \
            REDIS_TESTSERVER_STARTUP_DELAY_S
        self.__tmpfiles = []
        self.__server = None

    def _tmpfile(self, ghost=False):
        # We handle the allocated tmp files
        fd, fname = tempfile.mkstemp()
        os.close(fd)
        if ghost:
            os.unlink(fname)
        self.__tmpfiles.append(fname)
        return fname

    def _cleanup(self):
        for fname in [n for n in self.__tmpfiles if os.path.exists(n)]:
            logger.debug("removing temp file %s" % fname)
            os.unlink(fname)

    def __enter__(self):
        return self

    def __exit__(self, tb_type, tb_value, tb_object):
        self.stop()

    def get_pool_args(self):
        return {'host': self.server_config['bind'],
                'port': int(self.server_config['port']),
                'db': 0}

    def start(self, keypairs=None):

        self.server_config.update(keypairs if keypairs else {})

        # we need those defined
        mandatory = ['dir', 'dbfilename', 'pidfile', 'logfile']
        missing_keys = set(mandatory).difference(self.server_config.keys())
        if missing_keys:
            msg = "missing the following keys from config: "
            msg += "'%s'" % (", ".join(missing_keys))
            raise ValueError(msg)

        # the full qualified pathname to the redis server
        server_exe = utils.which(self.config['redis'])
        logger.debug("redis-server exe from: %s" % server_exe)

        # creating missing dirs
        dir_names = [self.server_config['dir']]
        for key in ['pidfile', 'logfile']:
            dir_names.append(os.path.dirname(self.server_config[key]))
        for dir_name in dir_names:
            if not os.path.exists(dir_name):
                logger.debug("creating dir: %s" % dir_name)
                os.makedirs(dir_name)
        for n in ['dir', 'dbfilename', 'pidfile', 'logfile']:
            logger.debug("using %s: %s" % (n, self.server_config[n]))

        # before we start we check if an istance is already running
        pool = redis.ConnectionPool(**self.get_pool_args())
        connection = redis.Redis(connection_pool=pool)
        try:
            connection.ping()
        except redis.ConnectionError:
            pass
        else:
            msg = "a redis server instance is listening at: "
            msg += str(self.get_pool_args())
            logger.warn(msg)

        # the main redis config file (generated on the fly
        # from the server_config dict)
        config_file = self._tmpfile()
        fp = open(config_file, "w")
        fp.write(KeyPairMapping.to_str(self.server_config))
        fp.flush
        fp.close()
        logger.debug("temp config file: %s" % config_file)

        # Launching the server
        args = [server_exe, config_file]
        logger.debug("redis server launch command: %s" % ' '.join(args))

        self.__server = subprocess.Popen(args)
        logger.debug("redis server started with pid %i" % self.__server.pid)

        if 'startup_delay_s' in self.config:
            msg = "waiting %is before listening" % \
                (self.config['startup_delay_s'])
            logger.debug(msg)
            time.sleep(self.config['startup_delay_s'])
        msg = str(self.__class__.__name__)
        msg += " listening at %(bind)s:%(port)s" % self.server_config
        logger.debug(msg)

    def stop(self):
        if not self.__server:
            return

        if hasattr(self.__server, "terminate"):
            self.__server.terminate()
        else:
            os.kill(self.__server.pid, signal.SIGTERM)

        self.__server.wait()
        self._cleanup()
        self.__server = None


class TestServer(TestServerBase):
    def start(self, keypairs=None):
        newdata = keypairs.copy() if keypairs else {}

        # we're trying to follow the redis way to configure files here.
        # pid/log files can be absolute files -> no problem
        # pid/log files can be relative to a working_dir if provided
        # dbfilename is always relative to working_dir
        #       if working_dir is not setit must set it

        working_dir = newdata.get("dir",
                                  self.server_config.get("dir", os.getcwd()))
        working_dir = os.path.abspath(working_dir)

        for name in ['pidfile', 'logfile']:
            filename = newdata.get(name, self.server_config.get(name, None))
            if not filename:
                filename = self._tmpfile()
            if not os.path.isabs(filename):
                filename = os.path.join(working_dir, filename)
            newdata[name] = filename

        dbfilepath = newdata.get("dbfilename",
                                 self.server_config.get("dbfilename", None))
        if not dbfilepath:
            dbfilepath = self._tmpfile(ghost=True)

        dbfilename = dbfilepath
        if os.path.isabs(dbfilepath):
            # We reassign working dir here
            working_dir = os.path.dirname(dbfilepath)
            dbfilename = os.path.basename(dbfilepath)

        newdata['dir'] = working_dir
        newdata['dbfilename'] = dbfilename

        return super(TestServer, self).start(newdata)
