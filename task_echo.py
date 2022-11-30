#
# example task that sends UDP packets to the device and logs the reponse
#

import asyncio, socket, datetime, functools

task_name = None
bglobals = None
timer = None
do_stop = False
echo_tasks = {}
udp_handler = None

async def send_ping(task_dict):
	global udp_handler
	
	while not do_stop:
		await asyncio.sleep(float(task_dict.get("interval", 1)))
		if not udp_handler:
			continue
		
		if bglobals["args"].verbose:
			bglobals["bsprint"](task_name, "ping "+task_dict["dest"]+":"+task_dict["port"])
		
		if udp_handler.future:
			if udp_handler.future.exception():
				bglobals["bsprint"](task_name, udp_handler.future.exception())
		
		if udp_handler.transport:
			udp_handler.transport.sendto(b"ping", (task_dict["dest"], int(task_dict["port"])))

class UDPHandler(asyncio.DatagramProtocol):
	def __init__(self):
		super().__init__()
		self.transport = None
	
	def connection_made(self, transport):
		self.transport = transport
	
	def datagram_received(self, data, addr):
		# send the response to the bootstats core
		ts = datetime.datetime.now().timestamp()
		
		if bglobals["args"].verbose:
			bglobals["bsprint"](task_name, "rx", data)
		
		bglobals["mrun"].newLine(ts, data, source="task_"+task_name)
	
	def error_received(self, exc):
		bglobals["bsprint"](task_name, "error", exc)

# called by bootstats when the task should start
def start(trigger_name, task_dict):
	import socket
	global timer, echo_tasks, udp_handler
	
	if task_dict["name"] in echo_tasks:
		if bglobals["args"].verbose:
			bglobals["bsprint"](task_dict["name"], "already started")
		
		return
	
	if "port" not in task_dict:
		task_dict["port"] = "1234"
	if "dest" not in task_dict:
		task_dict["dest"] = "10.0.0.2"
	
	if bglobals["args"].verbose:
		bglobals["bsprint"](task_dict["name"], "listen on", task_dict.get("src_ip", '0.0.0.0'), int(task_dict["port"]))
	
	# start UDP listening socket
	udp_handler = UDPHandler()
	echo_tasks[task_dict["name"]] = udp_handler
	t = bglobals["eloop"].create_datagram_endpoint(lambda: udp_handler, local_addr=(task_dict.get("src_ip", '0.0.0.0'), int(task_dict["port"])))
	udp_handler.future = asyncio.run_coroutine_threadsafe(t, bglobals["eloop"])
	
	# start UDP sender
	timer = bglobals["Timer"](float(task_dict.get("initial", 0)), functools.partial(send_ping, task_dict))

def stop(trigger_name, task_dict):
	global echo_tasks
	
	if timer:
		timer.cancel()
		do_stop = True
	
	if task_dict and echo_tasks and task_dict["name"] in echo_tasks:
		echo_tasks[task_dict["name"]].transport.close()
		del echo_tasks[task_dict["name"]]

# called by bootstats at startup to initialize the task
def init(bootstats_globals, name):
	global task_name, bglobals
	
	task_name = name
	bglobals = bootstats_globals

def finish():
	if timer:
		stop(None, None)
