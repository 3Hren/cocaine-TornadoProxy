#!/usr/bin/env python

import random
import os
import atexit
import sys
import logging
from collections import defaultdict

from tornado import web
from tornado import ioloop
import tornado.options
import msgpack

from cocaine.services import Service
from cocaine.futures.chain import ChainFactory
from cocaine.exceptions import ServiceError

logger = logging.getLogger("tornado.application")
logger.setLevel(logging.INFO)

class Daemon(object):

    def __init__(self, pidfile, stdin='/dev/null', stdout='/dev/null', stderr='/dev/null'):
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.pidfile = pidfile

    def daemonize(self):
        """Double-fork magic"""
        try:
            pid = os.fork()
            if pid > 0:
                sys.exit(0)
        except OSError, err:
            sys.stderr.write("First fork failed: %d (%s)\n" % (err.errno, err.strerror))
            sys.exit(1)
        # decouple from parent environment
        os.chdir("/")
        os.setsid()
        os.umask(0)

        # Second fork
        try:
            pid = os.fork()
            if pid > 0:
                sys.exit(0)
        except OSError, err:
            sys.stderr.write("Second fork failed: %d (%s)\n" % (err.errno, err.strerror))
            sys.exit(1)
            
        sys.stdout.flush()
        sys.stderr.flush()
        si = file(self.stdin, 'r')
        so = file(self.stdout, 'w')
        se = file(self.stderr, 'w')
        os.dup2(si.fileno(), sys.stdin.fileno())
        os.dup2(so.fileno(), sys.stdout.fileno())
        os.dup2(se.fileno(), sys.stderr.fileno())

        #write PID file
        atexit.register(self.delpid)
        pid = str(os.getpid())
        file(self.pidfile,'w').write("%s\n" % pid)

    def delpid(self):
        try:
            os.remove(self.pidfile)
        except Exception, err:
            pass

    def start(self, *args):
        """
        Start  the daemon
        """

        try:
            pf = file(self.pidfile, 'r')
            pid = int(pf.read().strip())
            pf.close()
        except IOError:
            pid = None

        if pid:
            msg = "pidfile %s has been already existed. Exit.\n"
            sys.stderr.write(msg % self.pidfile)
            sys.exit(1)

        self.daemonize()
        self.run(*args)

    def stop(self):
        """
        Stop daemon.
        """
        try:
            pf = file(self.pidfile, 'r')
            pid = int(pf.read().strip())
            pf.close()
        except IOError:
            pid = None

        if not pid:
            msg = "pidfile %s doesnot exist. Exit.\n"
            sys.stderr.write(msg % self.pidfile)
            sys.exit(1)

        #Kill
        try:
            while True:
                os.kill(pid, SIGTERM)
                time.sleep(0.5)
        except OSError, err:
            err = str(err)
            if err.find("No such process") > 0:
                if os.path.exists(self.pidfile):
                    os.remove(self.pidfile)
            else:
                print str(err)
                sys.exit(1)


    def restart(self, *args):
        self.stop()
        self.start(*args)

    def run(self, *args):
        pass

SERVICE_CACHE_COUNT = 5
tornado.options.define("port", default=8088, type=int, help="listening port number")
tornado.options.define("count", default=SERVICE_CACHE_COUNT, type=int, help="count of instances per service")
tornado.options.define("daemon", default=False, type=bool, help="daemonize")
tornado.options.define("pidfile", default="/var/run/tornado", type=str, help="pid")
tornado.options.parse_command_line()


def gen(obj):
    headers = yield
    chunk = headers.get()
    for header, value in chunk['headers']:
        obj.add_header(header, value)
    obj.set_status(chunk['code'])
    body = yield
    obj.write(body.get())
    obj.finish()

def pack_httprequest(request):
    d = dict()
    d['meta'] = { "cookies" : dict(request.cookies),
                  "headers" : request.headers,
                  "host" : request.host,
                  "method" : request.method,
                  "path_info" : request.path,
                  "query_string" : request.query,
                  "remote_addr" : request.remote_ip,
                  "url" : request.uri,
                  "files" : request.files
                }
    d['body'] = request.body
    d['request'] = dict((param, value[0]) for param, value in request.arguments.iteritems())
    return d


class BasicHandler(web.RequestHandler):

    cache = defaultdict(list)

    def get_service(self, name):
        while True:
            if len(self.cache['key']) < tornado.options.options.count:
                try:
                    created = [Service(name, raise_reconnect_failure=False) for _ in xrange(0, tornado.options.options.count - len(self.cache[name]))]
                    [logger.info("Connect to app: %s endpoint %s " % (app.servicename, app.service_endpoint))
                                    for app in created]
                    self.cache[name].extend(created)
                except Exception as err:
                    logger.error(str(err))
                    return None
            chosen = random.choice(self.cache[name])
            if chosen.connected:
                return chosen
            else:
                logger.warning("Service %s disconnected %s" % (chosen.servicename,
                                                                    chosen.service_endpoint))
                self.cache[name].remove(chosen)


    @web.asynchronous
    def get(self, name, event):
        s = self.get_service(name)
        if s is None:
            self.send_error(status_code=404)
        else:
            d = pack_httprequest(self.request)
            fut = s.invoke(event, msgpack.packb(d))
            g = gen(self)
            g.next()
            fut.then(g.send).run()

    @web.asynchronous
    def post(self, name, event):
        s = self.get_service(name)
        if s is None:
            self.send_error(status_code=404)
        else:
            d = pack_httprequest(self.request)
            fut = s.invoke(event, msgpack.packb(d))
            g = gen(self)
            g.next()
            fut.then(g.send).run()

def main():
    application = web.Application([
        (r"/([^/]*)/([^/]*)", BasicHandler),
        ])
    application.listen(tornado.options.options.port, no_keep_alive=False)
    ioloop.IOLoop.instance().start()

if __name__ == "__main__":
    if tornado.options.options.daemon:
        d = Daemon(tornado.options.options.pidfile+str(tornado.options.options.port))
        d.run = main
        d.start()
    else:
        main()
