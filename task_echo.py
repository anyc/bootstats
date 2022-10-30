#
# example task that sends UDP packets to the device and logs the reponse
#

import asyncio, socket, datetime, functools

task_name = None
bglobals = None
timer = None
do_stop = False

async def send_ping(task_dict):
	sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
	
	while not do_stop:
		await asyncio.sleep(1)
		
		sock.sendto(b"ping", (task_dict["dest"], int(task_dict["port"])))

class UDPHandler(asyncio.DatagramProtocol):
	def __init__(self):
		super().__init__()

	def connection_made(self, transport):
		self.transport = transport

	def datagram_received(self, data, addr):
		# send the response to the bootstats core
		ts = datetime.datetime.now().timestamp()
		bglobals["mrun"].newLine(ts, data, source="task_"+task_name)

# called by bootstats when the task should start
def start(trigger_name, task_dict):
	global timer
	
	if "port" not in task_dict:
		task_dict["port"] = "1234"
	if "dest" not in task_dict:
		task_dict["dest"] = "127.0.0.1"
	
	# start UDP listening socket
	t = bglobals["eloop"].create_datagram_endpoint(UDPHandler, local_addr=('0.0.0.0', int(task_dict["port"])))
	asyncio.run_coroutine_threadsafe(t, bglobals["eloop"])
	
	# start UDP sender
	timer = bglobals["Timer"](1, functools.partial(send_ping, task_dict))

def stop(trigger_name, task_dict):
	if timer:
		timer.cancel()
		do_stop = True

# called by bootstats at startup to initialize the task
def init(bootstats_globals, name):
	global task_name, bglobals
	
	task_name = name
	bglobals = bootstats_globals

def finish():
	if timer:
		stop(None, None)
