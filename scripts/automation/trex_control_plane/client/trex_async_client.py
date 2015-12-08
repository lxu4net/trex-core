#!/router/bin/python

try:
    # support import for Python 2
    import outer_packages
except ImportError:
    # support import for Python 3
    import client.outer_packages
from client_utils.jsonrpc_client import JsonRpcClient, BatchMessage

from common.text_opts import *

import json
import threading
import time
import datetime
import zmq
import re

from common.trex_stats import *
from common.trex_streams import *

# basic async stats class
class CTRexAsyncStats(object):
    def __init__ (self):
        self.ref_point = None
        self.current = {}
        self.last_update_ts = datetime.datetime.now()

    @staticmethod
    def format_num (size, suffix = ""):

        for unit in ['','K','M','G','T','P']:
            if abs(size) < 1000.0:
                return "%3.2f %s%s" % (size, unit, suffix)
            size /= 1000.0

        return "NaN"

    def update (self, snapshot):

        #update
        self.last_update_ts = datetime.datetime.now()

        self.current = snapshot

        if self.ref_point == None:
            self.ref_point = self.current

    def clear(self):
        self.ref_point = self.current
        

    def get(self, field, format=False, suffix=""):

        if not field in self.current:
            return "N/A"

        if not format:
            return self.current[field]
        else:
            return self.format_num(self.current[field], suffix)

    def get_rel (self, field, format=False, suffix=""):
        if not field in self.current:
            return "N/A"

        if not format:
            return (self.current[field] - self.ref_point[field])
        else:
            return self.format_num(self.current[field] - self.ref_point[field], suffix)


    # return true if new data has arrived in the past 2 seconds
    def is_online (self):
        delta_ms = (datetime.datetime.now() - self.last_update_ts).total_seconds() * 1000
        return (delta_ms < 2000)

# describes the general stats provided by TRex
class CTRexAsyncStatsGeneral(CTRexAsyncStats):
    def __init__ (self):
        super(CTRexAsyncStatsGeneral, self).__init__()


# per port stats
class CTRexAsyncStatsPort(CTRexAsyncStats):
    def __init__ (self):
        super(CTRexAsyncStatsPort, self).__init__()

    def get_stream_stats (self, stream_id):
        return None

# stats manager
class CTRexAsyncStatsManager():
    def __init__ (self):

        self.general_stats = CTRexAsyncStatsGeneral()
        self.port_stats = {}


    def get_general_stats(self):
        return self.general_stats

    def get_port_stats (self, port_id):

        if not str(port_id) in self.port_stats:
            return None

        return self.port_stats[str(port_id)]

   
    def update(self, data):
        self.__handle_snapshot(data)

    def __handle_snapshot(self, snapshot):

        general_stats = {}
        port_stats = {}

        # filter the values per port and general
        for key, value in snapshot.iteritems():
            
            # match a pattern of ports
            m = re.search('(.*)\-([0-8])', key)
            if m:

                port_id = m.group(2)
                field_name = m.group(1)

                if not port_id in port_stats:
                    port_stats[port_id] = {}

                port_stats[port_id][field_name] = value

            else:
                # no port match - general stats
                general_stats[key] = value

        # update the general object with the snapshot
        self.general_stats.update(general_stats)

        # update all ports
        for port_id, data in port_stats.iteritems():

            if not port_id in self.port_stats:
                self.port_stats[port_id] = CTRexAsyncStatsPort()

            self.port_stats[port_id].update(data)





class CTRexAsyncClient():
    def __init__ (self, server, port, stateless_client):

        self.port = port
        self.server = server
        self.stateless_client = stateless_client

        self.raw_snapshot = {}

        self.stats = CTRexAsyncStatsManager()

        self.connected = False
 
    # connects the async channel
    def connect (self):

        if self.connected:
            self.disconnect()

        self.tr = "tcp://{0}:{1}".format(self.server, self.port)
        print "\nConnecting To ZMQ Publisher On {0}".format(self.tr)

        #  Socket to talk to server
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.SUB)


        # before running the thread - mark as active
        self.active = True
        self.alive = False
        self.t = threading.Thread(target = self._run)

        # kill this thread on exit and don't add it to the join list
        self.t.setDaemon(True)
        self.t.start()

        self.connected = True


        # wait for data streaming from the server
        timeout = time.time() + 5
        while not self.alive:
            time.sleep(0.01)
            if time.time() > timeout:
                self.disconnect()
                return False, "*** [subscriber] - no data flow from server at : " + self.tr

        return True, ""


    # disconnect
    def disconnect (self):
        if not self.connected:
            return

        # signal that the context was destroyed (exit the thread loop)
        self.context.term()

        # mark for join and join
        self.active = False
        self.t.join()

        # done
        self.connected = False

    # thread function
    def _run (self):

        # no data yet...
        self.alive = False

        # socket must be created on the same thread 
        self.socket.connect(self.tr)
        self.socket.setsockopt(zmq.SUBSCRIBE, '')
        self.socket.setsockopt(zmq.RCVTIMEO, 5000)

        while self.active:
            try:

                line = self.socket.recv_string()

                if not self.alive:
                    self.stateless_client.on_async_alive()
                    self.alive = True

            # got a timeout - mark as not alive and retry
            except zmq.Again:

                if self.alive:
                    self.stateless_client.on_async_dead()
                    self.alive = False

                continue

            except zmq.ContextTerminated:
                # outside thread signaled us to exit
                self.alive = False
                break

            msg = json.loads(line)

            name = msg['name']
            data = msg['data']
            type = msg['type']
            self.raw_snapshot[name] = data

            self.__dispatch(name, type, data)

        
        # closing of socket must be from the same thread
        self.socket.close(linger = 0)


    def get_stats (self):
        return self.stats

    def get_raw_snapshot (self):
        return self.raw_snapshot

    # dispatch the message to the right place
    def __dispatch (self, name, type, data):
        # stats
        if name == "trex-global":
            self.stateless_client.handle_async_stats_update(data)
        # events
        elif name == "trex-event":
            self.stateless_client.handle_async_event(type, data)
        else:
            pass

