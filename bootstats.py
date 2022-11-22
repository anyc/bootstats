#! /usr/bin/env python3
#
# bootstats - analyze boot time consumption
#
# Author: Mario Kicherer <dev@kicherer.org>
# License: MIT
#

import sys, argparse, datetime, time, threading, signal, functools, os
import configparser, pprint
from math import floor
from re import match as re_match

import asyncio

## env variable to enable asyncio debug output
# PYTHONASYNCIODEBUG=1

global_ts_start = datetime.datetime.now().timestamp()
global_stop = False

adaptive_diff_output = 1

def bsprint(*args, **kwargs):
	if "file" in kwargs:
		f = kwargs["file"]
	else:
		f = sys.stdout
	if "ts" in kwargs:
		dt = datetime.datetime.fromtimestamp(kwargs["ts"])
		del kwargs["ts"]
	else:
		dt = datetime.datetime.now()
	print(dt.strftime("%H:%M:%S.%f "), end="", file=f)
	if "diff" in kwargs:
		diff = kwargs["diff"]
		del kwargs["diff"]
		
		if diff is not None:
			global adaptive_diff_output
			
			minutes, seconds = divmod(diff, 60)
			hours, minutes = divmod(minutes, 60)
			seconds = floor(seconds)
			
			if adaptive_diff_output:
				if hours or adaptive_diff_output > 3:
					print("%02d:" % hours, end="", file=f)
					adaptive_diff_output = 4
				if minutes or adaptive_diff_output > 2:
					print("%02d:" % minutes, end="", file=f)
					if adaptive_diff_output < 3:
						adaptive_diff_output = 3
				if seconds or adaptive_diff_output > 1:
					print("%02d" % seconds, end="", file=f)
					if adaptive_diff_output < 2:
						adaptive_diff_output = 2
				print(".%06d" % ((diff % 1)*1000000), end="", file=f)
			else:
				print("%02d:%02d:%02d.%06d" % (hours, minutes, floor(seconds), (diff % 1)*1000000), end="", file=f)
	print("| ", end="", file=f)
	print(*args, **kwargs)

parser = argparse.ArgumentParser()

# sigrok options
parser.add_argument("--sr_driver", help="default: fx2lafw")
parser.add_argument("--sr_device", help="default: first available device")
parser.add_argument("--sr_channels")
parser.add_argument("--sr_samplerate")
parser.add_argument("--sr_scan", action="store_true", help="only show available devices for sigrok")
parser.add_argument("--sr-monitor", action="store_true", help="only dump events of the given sigrok channels")

parser.add_argument("--poweron", default="", help="command to power on the device")
parser.add_argument("--poweroff", default="", help="command to power off the device")
parser.add_argument("--manual-power", action="store_true", help="this application will wait until the device is powered on or off")
parser.add_argument("--sysrq-reboot", action="store_true", help="Send SYSRQ reboot sequence to restart device (not recommended)")

parser.add_argument("--serial-device", default="/dev/ttyUSB0")
parser.add_argument("--serial-baudrate", default=115200)
parser.add_argument("--reconnect-serial", action="store_true", help="handle case when serial device is powered by test device")

parser.add_argument("--iterations", default=1)
parser.add_argument("--min-duration", default=0.1, help="ignore cycles shorter than this")
parser.add_argument("--cooldown", default="0.5", help="time to wait after a power off until power will be restored")

parser.add_argument("-c", "--config")
parser.add_argument("--trigger", help="strings to look for in serial output")

parser.add_argument("--show-console", action="store_true")
parser.add_argument("--show-console-diff", action="store_true")
parser.add_argument("--color", action="store_true")
parser.add_argument("--serial-log-file", help="store received serial output in a file")
parser.add_argument("--pipe", help="create a named pipe that receives a copy of the serial output from the device")

parser.add_argument("--ref-file", help="provide a reference file with previously measured values")
parser.add_argument("--show-reference", action="store_true", help="also show values from reference file")

parser.add_argument("--default-source", default="serial")

parser.add_argument("-v", "--verbose", action="count", default=0)

args = parser.parse_args()

if args.config is None and os.path.isfile("bootstats.cfg"):
	args.config = "bootstats.cfg"

if args.config:
	if args.verbose:
		bsprint("parsing", args.config)
	config = configparser.ConfigParser()
	config.read(args.config)
	
	if "general" in config:
		for action in parser._actions:
			for arg in action.option_strings:
				name = arg.lstrip("-")
				if (
					name in config["general"]
					and (
						not hasattr(args, name)
						or getattr(args, name) == action.default
						)
					):
					setattr(args, name.replace("-", "_"), config["general"][name])

args.cooldown = float(args.cooldown)

if args.pipe:
	os.mkfifo(args.pipe)
	named_pipe = open(args.pipe, 'w')
else:
	named_pipe = None

try:
	if not args.color:
		raise ImportError()
	from termcolor import colored as color
except ImportError:
	def color(text, *args, **kwargs):
		return text

class Timer:
	def __init__(self, timeout, callback):
		self._timeout = timeout
		self._callback = callback
		self._task = asyncio.run_coroutine_threadsafe(self._job(), eloop)
	
	async def _job(self):
		await asyncio.sleep(self._timeout)
		await self._callback()
	
	def cancel(self):
		self._task.cancel()

available_tasks = {}
for fname in os.listdir("."):
	r = re_match("^task_([-_0-9a-z]+).py$", fname)
	if r:
		# eval does not like async
		if False:
			with open(fname) as f:
				eval(f.read())
		
		from importlib.machinery import SourceFileLoader
		task = SourceFileLoader(name, fname).load_module()
		task.init(globals(), r.group(1))
		available_tasks[r.group(1)] = { "module": task, "name": r.group(1) }

class MRun():
	def __init__(self):
		self.history = {}
		self.mpoints = {}
		self.mintervals = {}
		self.tasks = {}
		self.data = b""
		
		self.start_ts = None
		self.match_in_iteration = False
		self.powered = None
		self.measuring = False
		self.delayed_poweroff_task = None
		self.start_task = None
		
		triggers = {}
		
		#
		# setups triggers and intervals
		#
		
		if args.trigger:
			arr = args.trigger.split(":")
			if len(arr) > 1:
				name = arr[0]
				trigger = ":".join(arr[1:])
			else:
				name = ""
				trigger = args.trigger
			self.mpoints[name] = { "trigger": trigger.encode(), "config": {} }
			if self.mpoints[name]["trigger"] in triggers:
				triggers[self.mpoints[name]["trigger"]].append(name)
			else:
				triggers[self.mpoints[name]["trigger"]] = [name]
		
		for sect in config.sections():
			if sect.startswith("trigger_"):
				name = sect[len("trigger_"):]
				
				self.mpoints[name] = { "config": config[sect] }
				if "trigger" in config[sect]:
					self.mpoints[name]["trigger"] = config[sect]["trigger"].encode()
				else:
					self.mpoints[name]["trigger"] = name.encode()
				
				if self.mpoints[name]["trigger"] in triggers:
					triggers[self.mpoints[name]["trigger"]].append(name)
					if args.verbose:
						bsprint("found duplicate trigger:", self.mpoints[name]["trigger"], triggers[self.mpoints[name]["trigger"]])
				else:
					triggers[self.mpoints[name]["trigger"]] = [name]
				
				if config[sect].get("name", None):
					self.mpoints[name]["name"] = config[sect].get("name")
				else:
					self.mpoints[name]["name"] = name.replace("_", " ")
			if sect.startswith("interval_"):
				name = sect[len("interval_"):]
				
				self.mintervals[name] = { "config": config[sect] }
				
				if config[sect].get("name", None):
					self.mintervals[name]["name"] = config[sect].get("name")
				else:
					self.mintervals[name]["name"] = name.replace("_", " ")
				
				self.mintervals[name]["from"] = config[sect].get("from")
				self.mintervals[name]["to"] = config[sect].get("to")
			if sect.startswith("task_"):
				name = sect[len("task_"):]
				
				if name not in available_tasks:
					print("no task", name, "found", file=sys.stderr)
					sys.exit(1)
				
				self.tasks[name] = available_tasks[name]
				
				if config[sect].get("name", None):
					self.tasks[name]["name"] = config[sect].get("name")
				else:
					self.tasks[name]["name"] = name.replace("_", " ")
		
		for inter in self.mintervals:
			if "to" in self.mintervals[inter] and self.mintervals[inter]["to"] in self.mpoints:
				if "intervals" not in self.mpoints[self.mintervals[inter]["to"]]:
					self.mpoints[self.mintervals[inter]["to"]]["intervals"] = []
				self.mpoints[self.mintervals[inter]["to"]]["intervals"].append(inter)
		
		self.mpoints["power_on"] = { "name": "-- Power on --" }
		self.mpoints["power_off"] = { "name": "-- Power off --" }
		
		self.max_name_length=None
		for mpoint in self.mpoints:
			if self.max_name_length is None or len(self.mpoints[mpoint]["name"]) > self.max_name_length:
				self.max_name_length = len(self.mpoints[mpoint]["name"])
	
	def start(self):
		for mname in self.mpoints:
			self.mpoints[mname]["matched"] = False
		
		if args.serial_log_file:
			global serial_log_fd
			
			if not serial_log_fd:
				if args.verbose:
					bsprint("opening", args.serial_log_file)
				serial_log_fd = open(args.serial_log_file, "wb")
			
			global iterations
			serial_log_fd.write(f"\nrun {iterations}\n\n".encode())
		
		self.data = b""
		
		if mrun.powered and args.manual_power:
			bsprint("target is already powered, you might want to power cycle manually now")
		
		self.power_on()
	
	# initiate a new measurement run
	async def async_start(self, cooldown=False):
		if cooldown:
			await asyncio.sleep(args.cooldown)
		
		self.start()
	
	def powerChanged(self, state):
		global iterations, global_stop
		
		# we do not show a message every time as we could get spurious transitions after
		# powering off
		if self.measuring and sigrok_session:
			if state == "1":
				bsprint("power is on")
			elif state == "0":
				bsprint("power is off")
			else:
				bsprint(state)
		
		if state == "1":
			ts = datetime.datetime.now().timestamp()
			
			if args.poweron and not args.manual_power:
				if "power_on" not in self.history:
					self.history["power_on"] = []
				
				# always zero but necessary if it's used as start point of intervals
				self.history["power_on"].append(0)
			self.start_ts = ts
			self.last_ts = ts
		elif state == "0":
			ts = datetime.datetime.now().timestamp()
			
			if self.start_ts and self.measuring:
				self.measuring = False
				
				if not self.match_in_iteration:
					if args.verbose:
						bsprint("nothing measured, will ignore power cycle (%8.5f)" %(ts - self.start_ts))
					
					self.start_task = asyncio.run_coroutine_threadsafe(self.async_start(True), eloop)
				else:
					self.startNewIteration()
			else:
				self.measuring = False
			
			if self.start_ts and sigrok_session:
				if "power_off" not in self.history:
					self.history["power_off"] = []
				self.history["power_off"].append(ts - self.start_ts)
			
			if iterations >= int(args.iterations)-1:
				global_stop = True
				eloop.call_soon_threadsafe(eloop.stop)
		else:
			if args.verbose:
				bsprint("unexpected state change to", state)
	
	# new line received from serial device
	def newLine(self, ts, line, source=None):
		global named_pipe
		
		found = False
		
		if args.show_console or args.show_console_diff:
			diff = None
			if args.show_console_diff:
				if getattr(self, "last_line_ts", None):
					diff = ts - self.last_line_ts
				self.last_line_ts = ts
			
			#bsprint(bytes(filter(lambda x: x >= 32, line)).decode(), ts=ts)
			bsprint(line.decode(errors='ignore'), ts=ts, diff=diff)
		
		i = 0
		while named_pipe and i < 2:
			try:
				named_pipe.write(line.decode())
				named_pipe.flush()
				break
			except os.BrokenPipeError:
				named_pipe.close()
				named_pipe = open(args.pipe, 'w')
				i += 1
		
		if args.serial_log_file:
			global serial_log_fd
			
			if not serial_log_fd:
				if args.verbose:
					bsprint("opening", args.serial_log_file)
				serial_log_fd = open(args.serial_log_file, "wb")
			
			serial_log_fd.write(line)
		
		if self.start_ts is None:
			return
		
		trig_dicts = self.mpoints
		
		for mname, mdict in trig_dicts.items():
			if "config" in mdict:
				if "source" not in mdict["config"] and source != args.default_source:
					continue
				if "source" in mdict["config"] and mdict["config"]["source"] != source:
					continue
			
			if (
				"trigger" in mdict and line.find(mdict["trigger"]) > -1
				or "regexp" in mdict and re_match(mdict["regexp"], line)
				):
				before = trig_dicts[mname]["config"].get("before", "")
				if before and before in self.history and len(self.history[before]) >= iterations+1:
					continue
				
				after = trig_dicts[mname]["config"].get("after", "")
				if after and (after not in self.history or len(self.history[after]) < iterations+1):
					continue
				
				found = True
				pretty_name = trig_dicts[mname].get("name")
				name = mname
				break
		
		if found:
			self.match_in_iteration = True
			
			if name not in self.history:
				self.history[name] = []
			
			# have we seen this trigger already in this run?
			if len(self.history[name]) > iterations:
				if int(trig_dicts[mname]["config"].get("multi_trigger", "0")):
					i = 2
					while True:
						new_name = name+"_"+str(i)
						if new_name not in self.history:
							self.history[new_name] = []
						if len(self.history[new_name]) == iterations:
							trig_dicts[new_name] = trig_dicts[name].copy()
							trig_dicts[new_name]["name"] += " "+str(i)
							name = new_name
							pretty_name = trig_dicts[new_name]["name"]
							break
						i += 1
				elif int(trig_dicts[mname]["config"].get("ignore_multiple_trigger", "0")):
					return
				else:
					bsprint(f"received \"{name}\" multiple times, ignoring (set multi_trigger=1 to accept multiple values)", file=sys.stderr)
					return
			
			print(color("%*s %10.6f  (delta %10.6f)" % (self.max_name_length, pretty_name, ts - self.start_ts, ts - self.last_ts), "blue"))
			
			self.last_ts = ts
			self.history[name].append(ts - self.start_ts)
			
			if "intervals" in trig_dicts[mname]:
				for inter_name in trig_dicts[mname]["intervals"]:
					inter = self.mintervals[inter_name]
					
					if inter_name not in self.history:
						self.history[inter_name] = []
					
					from_name = self.mintervals[inter_name]["from"]
					to_name = self.mintervals[inter_name]["to"]
					
					if from_name in self.history and to_name in self.history:
						if len(self.history[from_name]) == iterations+1 and len(self.history[to_name]) == iterations+1:
							self.history[inter_name].append(self.history[to_name][-1] - self.history[from_name][-1])
							
							print("%*s %10s  (delta %10.6f)" % (self.max_name_length, self.mintervals[inter_name]["name"], "", self.history[inter_name][-1]))
			
			trig_dicts[name]["matched"] = True
			
			if "start_task" in trig_dicts[mname]["config"]:
				tname = trig_dicts[mname]["config"]["start_task"]
				if tname in self.tasks:
					if args.verbose:
						bsprint("starting task", tname)
					self.tasks[tname]["module"].start(mname, self.tasks[tname])
			if "stop_task" in trig_dicts[mname]["config"]:
				tname = trig_dicts[mname]["config"]["stop_task"]
				if tname in self.tasks:
					if args.verbose:
						bsprint("stopping task", tname)
					self.tasks[tname]["module"].stop(mname, self.tasks[tname])
			
			# check if all triggers were matchewd during this run or if a "powerOff"
			# trigger matched
			stop = True
			for mname in trig_dicts:
				if "trigger" not in trig_dicts[mname] and "regexp" not in trig_dicts[mname]:
					continue
				if not trig_dicts[mname]["matched"]:
					stop = False
				elif trig_dicts[mname]["config"].get("powerCycle", "0") == "1":
					delay_poweroff = int(trig_dicts[mname]["config"].get("powerCycleAfter", "0"))
					stop = True
					break
			
			if stop:
				if args.verbose:
					bsprint("will stop measurement")
				
				if args.poweroff and not args.manual_power:
					if delay_poweroff > 0:
						if self.delayed_poweroff_task is None:
							if args.verbose:
								bsprint("will delay power-off by %d seconds" % delay_poweroff)
							self.delayed_poweroff_task = eloop.call_later(delay_poweroff, lambda: asyncio.ensure_future(self.delayed_poweroff()))
						elif args.verbose:
							bsprint("ignoring additional powerOff delay")
					else:
						self.power_off()
				elif args.manual_power:
					bsprint("you can turn off the device now", file=sys.stderr)
				elif args.sysrq_reboot:
					self.power_off(repower=False)
					self.startNewIteration(cooldown=False)
				else:
					bsprint("error, no method specified to restart target", file=sys.stderr)
					sys.exit(1)
	
	async def delayed_poweroff(self):
		self.power_off()
		self.delayed_poweroff_task = None
	
	def power_off(self, repower=True):
		self.measuring = False
		
		if self.powered:
			if args.verbose:
				bsprint("powering off")
			
			if args.poweroff:
				os.system(args.poweroff)
			if repower:
				self.startNewIteration()
			
			if not sigrok_session:
				self.powered = False
				self.powerChanged("0")
	
	def power_on(self):
		self.measuring = True
		
		if args.sysrq_reboot and not args.poweron and not args.manual_power:
			bsprint("will reboot using sysrq")
			self.powering_on_ts = datetime.datetime.now().timestamp()
			send_sysrq_reboot()
			
			self.powered = False
		
		if not self.powered:
			if args.poweron:
				if not args.manual_power:
					if args.verbose:
						bsprint("powering on")
				else:
					if self.powered:
						bsprint("you can turn the device off and on now", file=sys.stderr)
					else:
						bsprint("you can turn on the device now", file=sys.stderr)
			
			if args.poweron and not args.manual_power:
				self.powering_on_ts = datetime.datetime.now().timestamp()
				os.system(args.poweron)
			
			if not sigrok_session:
				self.powered = True
				self.powerChanged("1")
	
	def startNewIteration(self, cooldown=True):
		global iterations
		
		bsprint("iteration", iterations+1, "done")
		
		iterations += 1
		if iterations >= int(args.iterations):
			return
		
		self.match_in_iteration = False
		
		if cooldown:
			self.start_task = asyncio.run_coroutine_threadsafe(self.async_start(True), eloop)
		else:
			self.start()

mrun = MRun()
mainlock = threading.Lock()

iterations = 0
startup_counter = 0
serial_log_fd = None

eloop = asyncio.new_event_loop()
asyncio.set_event_loop(eloop)

def ask_exit(signame):
	bsprint("got signal %s: exit" % signame)
	global_stop = True
	eloop.stop()

if not args.sr_monitor:
	for signame in ('SIGINT', 'SIGTERM'):
		eloop.add_signal_handler(getattr(signal, signame),
								functools.partial(ask_exit, signame))

##############
# setup sigrok

if args.sr_scan or args.sr_driver or args.sr_device or args.sr_channels:
	import sigrok.core as sr
	from sigrok.core.classes import *

	if args.verbose:
		bsprint("setup sigrok")

	context = sr.Context_create()

	if args.sr_scan:
		print("Drivers and devices:")
		for name, driver in context.drivers.items():
			if args.sr_driver and args.sr_driver != name:
				continue
			
			print(" %-25s %s" % (driver.name, driver.long_name))
			devices = driver.scan()
			for device in devices:
				print("    %s - %s with %d channels: %s" % (device.driver.name, str.join(' ',
					[s for s in (device.vendor, device.model, device.version) if s]),
					len(device.channels), str.join(' ', [c.name for c in device.channels])))
		sys.exit(0)

	if not args.sr_driver:
		args.sr_driver = "fx2lafw"

	driver_spec = args.sr_driver.split(':')

	driver = context.drivers[driver_spec[0]]

	driver_options = {}
	for pair in driver_spec[1:]:
		name, value = pair.split('=')
		key = ConfigKey.get_by_identifier(name)
		driver_options[name] = key.parse_string(value)

	devices = driver.scan(**driver_options)

	if args.sr_device:
		sigrok_device = devices[int(args.sr_device)]
	elif len(devices) > 0:
		sigrok_device = devices[0]
	else:
		print("erorr, no sigrok device found (%s)" % str(driver_options))
		sys.exit(1)

	sigrok_device.open()
	
	if args.sr_samplerate:
		sigrok_device.config_set(ConfigKey.SAMPLERATE, int(args.sr_samplerate))
	elif args.verbose:
		bsprint("Sample rate: ", sigrok_device.config_get(ConfigKey.SAMPLERATE))

	if args.sr_channels:
		enabled_channels = set(args.sr_channels.split(','))
		for channel in sigrok_device.channels:
			channel.enabled = (channel.name in enabled_channels)

	#output_format = 'bits'
	output_format = 'csv'
	sigrok_output = context.output_formats[output_format].create_output(sigrok_device)

	lastline = None
	def datafeed_in(sigrok_device, packet):
		global lastline
		
		lines = sigrok_output.receive(packet)
		
		if args.sr_monitor:
			for line in lines.split("\n"):
				if line and lastline and lastline != line:
					print(line)
				lastline = line
			return
		
		if lastline is None:
			for line in lines.split("\n"):
				if not line:
					continue
				if line == "1":
					mrun.powered = True
				elif line == "0":
					mrun.powered = False
				
				lastline = line
			
			with mainlock:
				global startup_counter, eloop
				
				if args.verbose:
					bsprint("sigrok ready")
				
				startup_counter |= 1
				if startup_counter == 3:
					asyncio.run_coroutine_threadsafe(mrun.async_start(), eloop)
			
			return
		
		for line in lines.split("\n"):
			if not line:
				continue
			if lastline is None or lastline == "logic":
				if line == "1":
					mrun.powered = True
				elif line == "0":
					mrun.powered = False
			elif lastline != line:
				if line == "1":
					mrun.powered = True
				elif line == "0":
					mrun.powered = False
				mrun.powerChanged(line)
			
			lastline = line

	sigrok_session = None

	# looks like there is no way to integrate sigrok into asyncio, so we start a
	# thread for sigrok's event loop

	def tmain():
		global sigrok_session
		global eloop
		
		sigrok_session = context.create_session()

		sigrok_session.add_device(sigrok_device)

		sigrok_session.start()
		
		sigrok_session.add_datafeed_callback(datafeed_in)
		
		sigrok_session.run()

	if args.verbose:
		bsprint("starting sigrok thread")

	sr_thread = threading.Thread(target=tmain)
	sr_thread.start()
	
	if args.sr_monitor:
		sr_thread.join()
		sys.exit(0)
else:
	sigrok_session = None
	sr_thread = None
	sigrok_device = None
	
	if not args.manual_power:
		mrun.powered = True
	
	startup_counter |= 1
	if startup_counter == 3:
		asyncio.run_coroutine_threadsafe(mrun.async_start(), eloop)

if args.poweroff and not args.manual_power:
	if args.verbose:
		bsprint("initial cooldown", args.cooldown, "seconds")
	
	# make sure the device is off at the beginning
	mrun.powered=True
	mrun.power_off(repower=False)
	
	time.sleep(args.cooldown)

########
# setup the UART interface

delta_min = None

# Currently, we prefer a separate thread over serial_asyncio to avoid additional
# latency in case the main loop is busy.
use_serial_async = False

if use_serial_async:
	import serial_asyncio
	
	if args.verbose:
		bsprint("starting serial asyncio")

	class Output(asyncio.Protocol):
		def connection_made(self, transport):
			self.transport = transport
			
			if args.verbose:
				bsprint("UART connected")
			
			self.buf = b""
			self.last_ts = None
			
			with mainlock:
				global startup_counter, eloop
				
				if (startup_counter & 2) == 0:
					startup_counter |= 2
					if startup_counter == 3:
						asyncio.run_coroutine_threadsafe(mrun.async_start(), eloop)

		def data_received(self, data):
			global named_pipe, delta_min
			
			ts = datetime.datetime.now().timestamp()
			if self.last_ts and (delta_min is None or ts - self.last_ts < delta_min):
				delta_min = ts - self.last_ts
			self.last_ts = ts
			
			if args.verbose:
				bsprint("UART RX %d bytes" % len(data), ts=ts)
			
			i = 0
			while named_pipe and i < 2:
				try:
					named_pipe.write(data.decode())
					named_pipe.flush()
					break
				except os.BrokenPipeError:
					named_pipe.close()
					named_pipe = open(args.pipe, 'w')
					i += 1
			
			if args.serial_log_file:
				global serial_log_fd
				
				if not serial_log_fd:
					if args.verbose:
						bsprint("opening", args.serial_log_file)
					serial_log_fd = open(args.serial_log_file, "wb")
				
				serial_log_fd.write(data)
			
			self.buf += data
			while True:
				newline_idx = self.buf.find(b"\n")
				if newline_idx > -1:
					if mrun.measuring:
						mrun.newLine(ts, self.buf[:newline_idx], source="serial")
					self.buf = self.buf[newline_idx+1:]
				else:
					break
		
		def connection_lost(self, exc):
			global startup_counter
			
			if args.verbose:
				bsprint("UART connection lost")
			
			with mainlock:
				startup_counter = startup_counter & (~2)
			
			if args.reconnect_serial:
				connect_uart()
			else:
				asyncio.get_event_loop().stop()

	coro_task = None
	def connect_serial_device():
		global coro_task
		
		if args.verbose:
			bsprint("connecting serial device %s ..." % args.serial_device)
		coro = serial_asyncio.create_serial_connection(eloop, Output, args.serial_device, baudrate=args.serial_baudrate)
		coro_task = eloop.create_task(coro)
	
	wait_task = None
	def connect_uart():
		global wait_task, startup_counter
		
		if os.path.exists(args.serial_device):
			connect_serial_device()
		else:
			bsprint("device %s not present, will wait..." % args.serial_device)
			
			if args.reconnect_serial:
				with mainlock:
					if startup_counter == 1:
						startup_counter |= 2
						
						# we assume that the serial is only available when the board is powered, so
						# we start now even if serial is not present
						asyncio.run_coroutine_threadsafe(mrun.async_start(), eloop)
			
			async def wait_on_device():
				while True:
					if os.path.exists(args.serial_device):
						connect_serial_device()
						try:
							result = await coro_task
						except:
							bsprint("error opening %s, will try again ..." % args.serial_device)
							time.sleep(0.2)
							continue
						break
					
					await asyncio.sleep(0.1)
			
			wait_task = eloop.create_task(wait_on_device())

	connect_uart()
else:
	import serial, termios, fcntl, array, select
	
	# If we only read one byte at once (like other tools) we see continuously increasing
	# timestamps. However, these timestamps are not accurate as they only show when we
	# started to fetch the line from the kernel and not when it was received. On tested machines,
	# the kernel buffer was almost always filled. Hence, there is always an offset between
	# the real and the observed timestamp as the data waits in the kernel buffer.
	# If we always read all available bytes from the kernel, we might see the same timestamp
	# for multiple lines but this makes it obvious to the user that the timestamps are not
	# accurate. Due to the separate RX thread, we keep the kernel buffer empty most of the time,
	# which should give us more accurate timestamps.
	# If we could keep the kernel buffer empty most of the time in single_byte mode, this mode
	# should provide more accurate timestamps.
	single_byte = False
	
	def uart_tmain():
		global delta_min, startup_counter, eloop, global_stop
		
		ser = None
		while not uart_thread.stop and not global_stop:
			if ser:
				ser.close()
			
			while not uart_thread.stop and not global_stop:
				time.sleep(0.5)
				
				if os.path.exists(args.serial_device):
					if single_byte:
						timeout = None
					else:
						timeout = 0
					try:
						ser = serial.Serial(args.serial_device, args.serial_baudrate, timeout=timeout)
						uart_thread.ser = ser
					except:
						bsprint("error opening %s, will try again ..." % args.serial_device)
						continue
					break
				else:
					if args.reconnect_serial:
						with mainlock:
							if startup_counter == 1:
								startup_counter |= 2
								
								# we assume that the serial is only available when the board is powered, so
								# we start now even if serial is not present
								asyncio.run_coroutine_threadsafe(mrun.async_start(), eloop)
			
			if args.verbose:
				bsprint("UART connected")
			
			with mainlock:
				if (startup_counter & 2) == 0:
					startup_counter |= 2
					if startup_counter == 3:
						asyncio.run_coroutine_threadsafe(mrun.async_start(), eloop)
			
			last_ts = None
			ts = None
			buf_ts = None
			buf = b""
			while not uart_thread.stop and not global_stop:
				if single_byte:
					try:
						d = ser.read()
					except Exception as e:
						if args.verbose:
							bsprint("serial read failed: %s" % str(e))
						break
					
					if ts is None:
						ts = datetime.datetime.now().timestamp()
						
						if True:
							data_available = [ser.in_waiting]
						else:
							data_available = array.array('i', [0])
							fcntl.ioctl(ser.fileno(), termios.FIONREAD, data_available)
						
						if args.verbose > 1:
							bsprint("UART RX %d bytes" % data_available[0], ts=ts)
					
					if d == b"\n":
						if mrun.measuring:
							eloop.call_soon_threadsafe(functools.partial(mrun.newLine, ts, buf, source="serial"))
						
						if last_ts and (delta_min is None or ts - last_ts < delta_min):
							delta_min = ts - last_ts
						last_ts = ts
						
						buf = b""
						ts = None
					elif d != b"" and d[0] >= 32:
						buf += d
				else:
					while not uart_thread.stop and not global_stop:
						r, w, e = select.select([ser.fileno()], [], [], 0.5)
						if len(e) == 0 or ser.fileno() in r:
							break
					
					if e:
						break
					
					try:
						if True:
							data = ser.read(ser.in_waiting)
						else:
							data_available = array.array('i', [0])
							fcntl.ioctl(ser.fileno(), termios.FIONREAD, data_available)
							
							data = ser.read(data_available[0])
					except Exception as e:
						if args.verbose:
							bsprint("serial read failed: %s" % str(e))
						break
					
					#if len(data) != data_available[0]:
						#print("diff", len(data), data_available[0])
					
					#if ts is None:
					ts = datetime.datetime.now().timestamp()
					
					if last_ts and (delta_min is None or ts - last_ts < delta_min):
						delta_min = ts - last_ts
					last_ts = ts
					
					if args.verbose > 1:
						bsprint("UART RX %d bytes" % len(data), ts=ts)
					
					last_newline = 0
					for i in range(len(data)):
						if data[i] == ord("\n"):
							if buf:
								line_ts = buf_ts
							else:
								line_ts = ts
							
							if mrun.measuring:
								line = buf + data[last_newline:i]
								line = bytes(filter(lambda x: x >= 32, line))
								
								#mrun.newLine(line_ts, line)
								
								eloop.call_soon_threadsafe(functools.partial(mrun.newLine, line_ts, line, source="serial"))
							
							buf = b""
							last_newline = i+1
					
					if last_newline < len(data):
						buf = data[last_newline:]
						buf_ts = ts
		
		if args.verbose:
			bsprint("serial thread stopped")
	
	uart_thread = threading.Thread(target=uart_tmain)
	uart_thread.stop = False
	uart_thread.ser = None
	uart_thread.start()
	
	def send_sysrq_reboot():
		from time import sleep
		secs = 1
		
		uart_thread.ser.send_break(secs)
		uart_thread.ser.write(b"u")
		uart_thread.ser.send_break(secs)
		uart_thread.ser.write(b"s")
		uart_thread.ser.send_break(secs)
		uart_thread.ser.write(b"b")

if args.verbose:
	bsprint("entering event loop")

if False:
	# TODO should we set a global exception handler?
	
	def custom_exception_handler(loop, context):
		eloop.default_exception_handler(context)
		
		print(context)
		loop.stop()
	
	eloop.set_exception_handler(custom_exception_handler)

if True:
	from systemd import journal
	
	j = journal.Reader()
	j.log_level(journal.LOG_INFO)
	#j.add_match(SYSLOG_IDENTIFIER="kernel")
	#j.add_match("_SYSTEMD_UNIT=hos
	j.seek_tail()
	j.get_previous()
	
	async def process_journal(event):
		if not mrun.measuring:
			return
		
		ts = datetime.datetime.now().timestamp()
		
		mrun.newLine(ts, event["MESSAGE"].encode(), source="journald")
	
	def journal_event():
		j.process()
		for entry in j:
			asyncio.ensure_future(process_journal(entry))
	
	eloop.add_reader(j.fileno(), journal_event)

eloop.run_forever()

global_stop = True

if args.verbose:
	bsprint("event loop stopped")

for task in available_tasks.values():
	task["module"].finish()

eloop.run_until_complete(eloop.shutdown_asyncgens())

if mrun.delayed_poweroff_task:
	mrun.delayed_poweroff_task.cancel()
if mrun.start_task:
	mrun.start_task.cancel()
if use_serial_async:
	if wait_task:
		wait_task.cancel()
else:
	if uart_thread.ser:
		uart_thread.ser.close()
	uart_thread.join()

eloop.close()
if sigrok_session:
	sigrok_session.stop()
if sr_thread:
	sr_thread.join()
if sigrok_device:
	sigrok_device.close()

if args.pipe:
	named_pipe.close()
	os.unlink(args.pipe)

global_ts_end = datetime.datetime.now().timestamp()

if args.verbose:
	bsprint("measurements done after %d seconds" % (global_ts_end - global_ts_start))
	if delta_min is not None:
		bsprint("min time between serial RX: %.6f" % delta_min)

conv = {
	"avg": "%10.6f",
	"dur": "%10.6f",
	"dev": "%10.6f",
	"share": "%7d",
	"max_dev": "%10.6f",
	"min_val": "%10.6f",
	"max_val": "%10.6f",
	"weight": "%6d",
	}
stat_names = conv.keys()

results = {}
print(f"Results after {iterations} runs:")

for mpoint in mrun.history:
	avg = None
	weight = 0
	for ts in mrun.history[mpoint]:
		if avg is None:
			avg = ts
			weight = 1
			continue
		
		avg += ts
		weight += 1
	
	if weight == 0:
		print("no values for", mpoint, mrun.history[mpoint])
		continue
	
	avg = avg / weight
	
	if weight > 1:
		dev = 0
		max_dev = None
		min_val = None
		max_val = None
		for ts in mrun.history[mpoint]:
			dev += (avg - ts) ** 2
			if max_dev is None or abs(avg - ts) > max_dev:
				max_dev = abs(avg - ts)
			if min_val is None or ts < min_val:
				min_val = ts
			if max_val is None or ts > max_val:
				max_val = ts
		
		dev = (dev / (weight - 1)) ** 0.5
	else:
		dev = 0
		max_dev = 0
		min_val = 0
		max_val = 0
	
	if mpoint in mrun.mpoints:
		results[mpoint] = {"name": mrun.mpoints[mpoint]["name"], "dur": None}
		share = None
	else:
		results[mpoint] = {"name": mrun.mintervals[mpoint]["name"], "dur": avg, "avg": None}
		
		share = 0
		if "power_off" in mrun.history and len(mrun.history[mpoint]) == len(mrun.history["power_off"]):
			for i in range(len(mrun.history[mpoint])):
				ts = mrun.history[mpoint][i]
				share += ts / mrun.history["power_off"][i]
			
			share = share / len(mrun.history[mpoint]) * 100
	
	for var in stat_names:
		if var in locals() and var not in results[mpoint]:
			results[mpoint][var] = locals()[var]

results = dict(sorted(results.items(), key=lambda x: results[x[0]]["avg"] if results[x[0]]["avg"] is not None else 0))

# show the column headers
print("\t%-*s" % (mrun.max_name_length, "Id"), end=" ")
for var in stat_names:
	print((conv[var].replace(".6", "")[:-1]+"s") % var, end=" ")
print()

for mpoint in results:
	if mpoint == "power_on":
		continue
	
	if mpoint in mrun.mpoints:
		pretty_name = mrun.mpoints[mpoint].get("name", "")
	else:
		pretty_name = mrun.mintervals[mpoint].get("name", "")
	
	for key in results[mpoint]:
		if key == "name":
			continue
	
	print("\t%-*s" % (mrun.max_name_length, pretty_name), end=" ")
	for key in stat_names:
		if results[mpoint][key]:
			print(conv[key] % results[mpoint][key], end=" ")
		else:
			lconv = conv[key].replace(".6", "")[:-1]+"s"
			print(lconv % "", end=" ")
	print()

if args.ref_file:
	if not os.path.isfile(args.ref_file):
		import pprint
		
		if args.verbose:
			print("writing ref file")
		
		with open(args.ref_file, "w") as f:
			f.write(pprint.pformat(results, sort_dicts=False))
	else:
		from ast import literal_eval
		
		if args.verbose:
			print("reading ref file")
		
		ref_string = open(args.ref_file).read()
		
		ref = literal_eval(ref_string)
		
		print("Comparison with reference:", args.ref_file)
		for mpoint in results:
			if mpoint in mrun.mpoints:
				pretty_name = mrun.mpoints[mpoint].get("name", "")
			else:
				pretty_name = mrun.mintervals[mpoint].get("name", "")
			
			if mpoint not in ref:
				print("\t%-*s" % (mrun.max_name_length, pretty_name))
				continue
			
			if mpoint in ["power_on", "power_off"]:
				continue
			
			diff={ "mpoint": mpoint }
			for stat in stat_names:
				if results[mpoint][stat] is not None and ref[mpoint][stat] is not None:
					diff[stat] = results[mpoint][stat] - ref[mpoint][stat]
			
			print("\t%-*s" % (mrun.max_name_length, pretty_name), end=" ")
			for key in stat_names:
				if key in diff and diff[key]:
					print(conv[key] % diff[key], end=" ")
				else:
					lconv = conv[key].replace(".6", "")[:-1]+"s"
					print(lconv % "", end=" ")
			print()
		
		if args.show_reference:
			print("\nReference values:", args.ref_file)
			for mpoint in ref:
				if mpoint in ["power_on", "power_off"]:
					continue
				
				if mpoint in mrun.mpoints:
					pretty_name = mrun.mpoints[mpoint].get("name", "")
				else:
					pretty_name = mrun.mintervals[mpoint].get("name", "")
				
				print("\t%-*s" % (mrun.max_name_length, pretty_name), end=" ")
				for key in stat_names:
					if ref[mpoint][key]:
						print(conv[key] % ref[mpoint][key], end=" ")
					else:
						lconv = conv[key].replace(".6", "")[:-1]+"s"
						print(lconv % "", end=" ")
				print()
