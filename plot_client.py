#!/usr/bin/python

import sys, time, datetime, getopt, subprocess, os, threading, pexpect, tty, termios, re, matplotlib
import matplotlib.pyplot as plt
import numpy as np
import argparse
from matplotlib.font_manager import FontProperties
from mylib import *

UPDATE_INTERVAL = 1					
sem_data 		= threading.Semaphore(1) 	# semaphore for operations on data
stop 			= threading.Event() 		# set if the program is running
pause			= threading.Event()			# set if the graph is in pause
global t0 	# unix timestamp of the reference instant

"""
The graph keeps expanding until MAX_TIME_WINDOW [seconds],
then starts to slide and delete out-of-graph data
"""
MAX_TIME_WINDOW = 30

IPERF_REPORT_INTERVAL = 1

# time with no reports to considered a user as dead
DEATH_TOLERANCE = 2 * IPERF_REPORT_INTERVAL 

"""
Prepare the system to collect tcp flows information
"""
def insert_tcp_probe_module(tcp_server_port):
	# instruct linux to forget previous tcp sessions
	subprocess.call("sudo sysctl -w net.ipv4.tcp_no_metrics_save=1", shell=True) 
	# insert a new tcp_probe module
	subprocess.call("sudo modprobe tcp_probe port={} full=1".format(tcp_server_port), shell=True) 
	# obtain permits to modify the tcp-probe output file
	subprocess.call("sudo chmod 444 /proc/net/tcpprobe", shell=True) 


"""
Thread that execute, parse and write ping (rtt measure)
"""
def ping_thread(data, server_ip):
	cmd = "ping -i {} -D {}".format(UPDATE_INTERVAL, server_ip)

	samples = data["samples"][server_ip]

	for line in runPexpect(cmd):
		if stop.is_set():
			break
		"""
		example line: 
		1                   2  3     4    5              6           7      8
		[1437417582.711328] 64 bytes from 10.100.13.214: icmp_seq=21 ttl=64 time=0.104 ms
		"""
		cols = line.split(" ")
		if len(cols) == 9 and line[0] == "[": #only reports, not the final average			
			stamp = float((cols[0])[1:len(cols[0])-1])-t0
			cols2 = cols[7].split("=")
			
			if len(cols2) != 2:
				continue
			
			rtt = float(cols2[1])
			
			with sem_data:
				samples["t"].append(stamp)
				samples["val"].append(rtt)
				if rtt > data["max"]:
					data["max"] = rtt
					data["t_max"] = stamp


"""
Thread that execute, parse and write bwm-ng (bandwidth measure)
"""
def bwm_ng_thread(data, intf):
	cmd ="bwm-ng -u bits -T rate -t {} -I {} -d 0 -c 0 -o csv".format(UPDATE_INTERVAL*1000, intf)

	samples = data["samples"][intf]	

	for line in runPexpect(cmd):
		if stop.is_set():
			break
		"""
		example lines: 
		0          1    2       3         4         5     6   7     8      9      10 11 12  13  14 15
		1437515226;eth0;1620.00;123595.00;125215.00;24719;324;25.00;110.00;135.00;22;5;0.00;0.00;0;0
		1437515226;total;1620.00;123595.00;125215.00;24719;324;25.00;110.00;135.00;22;5;0.00;0.00;0;0

		0: unix timestamp	*
		1: interface
		2: bytes_out/s 		*
		3: bytes_in/s
		4: bytes_total/s
		5: bytes_in
		6: bytes_out
		7: packets_out/s
		8: packets_in/s
		9: packets_total/s
		10: packets_in
		11: packets_out
		12: errors_out/s
		13: errors_in/s
		14: errors_in
		15: errors_out 

		Timestamps has a resolution in seconds, so we take a report every second

		The first (like) 10 timestamps comes at 1ms distance, 
		the others every 1sec

		Also if passing the -u bits option, rate remains is in byte/s

		"""
		if line.find("total") != -1 : # only reports, not the total
			continue

		cols = line.split(";")
		stamp = int(cols[0])-t0 
		rate = float(cols[2])*8 # conversion byte/s --> bit/s
		with sem_data:
			samples["t"].append(stamp)
			samples["val"].append(rate)
			if rate > data["max"]:
				data["max"] = rate
				data["t_max"] = stamp

"""
Thread that execute, parse and write tcp-probe (congestion window measure)
"""
def tcp_probe_thread(data):
	cmd = "cat /proc/net/tcpprobe"

	# Create the dictionary for the summation of windows
	# data["samples"]["SUM"] = {"t":[], "val":[] }

	samples = data["samples"]

	cwnd_min = 0

	for line in runPexpect(cmd):
		if stop.is_set():
			break
		""" 
		example line:
		0           1                   2                  3  4          5          6  7  8      9 10
		0.000605295 10.100.13.162:45758 10.100.13.214:5001 32 0xf76a3a5b 0xf7692adb 48 41 292992 1 29312
		source code: http://lxr.free-electrons.com/source/net/ipv4/tcp_probe.c?v=3.12
		0: Time in seconds					*
		1: Source IP:Port 					*
		2: Dest IP: Port
		3: Packet length (bytes)
		4: snd_nxt
		5: snd_una
		6: snd_cwnd							*
		7: ssthresh 						
		8: snd_wnd 							
		9: srtt  							
		10: rcv_wnd (3.12 and later)
		"""

		cols = line.split(" ")
		stamp = float(cols[0])
		src = str(cols[1])
		cwnd = int(cols[6])

		with sem_data:	

			"""
			Since tcp-probe does not advise of a flow termination,
			we should declare it dead after some time without information
			and write a zero in that point.
			The min is to have a more realistic initial window ("zero")
			(notable only with low rates)
			"""
			cwnd_min = min(cwnd, cwnd_min)

			# if there is a new connection, create its record
			if src not in data["samples"]:
				samples[src]= {"t":[], "val":[]}

			# Save data 	
			samples[src]["t"].append(stamp)
			samples[src]["val"].append(cwnd)

			update_death_flows(samples, stamp, cwnd_min)

			if cwnd > data["max"]:
				data["max"] = cwnd
				data["t_max"] = stamp
			#update_cwnd_sum(data,stamp)

def update_death_flows(data, stamp, cwnd_min):
	"""
	data is data[key]["samples"]
	if the last sample is too far, 
	we create a null sample just after the last sample.
	The last sample is considered the dead point
	Apply this only to summation line
	"""
	for src in data:
		if len(data[src]["t"]) <= 0:
			continue

		if len(data[src]["t"]) > 0:
			# Take the last istant
			last_t = data[src]["t"][-1]
			# If it's too far, put a "zero" just after the last sample
			if abs(stamp - last_t) >= DEATH_TOLERANCE:
				data[src]["t"].append(last_t + IPERF_REPORT_INTERVAL) 	
				data[src]["val"].append(cwnd_min)	



# def update_cwnd_sum(data, stamp):
# 	"""
# 	The value of the sum for this instant is the
# 	real sample value plus all the previous samples of the other flows
# 	"""
# 	sum_tolerance = 0.5 # seconds to consider a flow for the sum of windows
# 	cwnd_sum = 0
# 	# For all sources
# 	for src in data["samples"]: 
# 		# if the source is not the sum and the last sample is close enough
# 		if(len(data["samples"][src]["t"])>0 
# 				and src!="SUM" 
# 				and stamp-data["samples"][src]["t"][-1]<sum_tolerance):
# 			# consider the sample in the summation
# 			cwnd_sum += data["samples"][src]["val"][-1]

# 	data["samples"]["SUM"]["t"].append(stamp) # temporal axis with all values	
# 	data["samples"]["SUM"]["val"].append(cwnd_sum)
	
# 	if cwnd_sum > data["max"]:
# 		data["max"] = cwnd_sum
# 		data["t_max"] = stamp

def keyboard_listener_thread(server_ip, tcp_server_port, udp_server_port):
	tcp_connections = [] 
	udp_connection = None
	udp_bandwidth = 0

	help_string = "\nKeyboard listener started\
	\nCommands:\
	\n i : info\
	\n s : save screenshot\
	\n p : pause plotting\
	\n +#t : create # TCP flows\
	\n -#t : kill # TCP flows\
	\n +#u : create 1 UDP flows with rate # Mbps\
	\n kt  : kill all TCP flows\
	\n ku  : kill UDP flow\
	\n ka  : kill all flows\
	\n q   : quit the program"

	print help_string

	create_tcp_flows 	= re.compile("\+([0-9]{1,4})t")
	kill_tcp_flows 		= re.compile("\-([0-9]{1,4})t")
	create_udp_flow 	= re.compile("\+([0-9]{1,4})u")

	kill_tcp = "kt"
	kill_udp = "ku"
	kill_all = "ka"
	quit = "q"
	info = "i"
	save = "s"
	p = "p"
	cmd = ""

	try:
		while cmd != quit:  
			cmd = str(raw_input("\nCommand: ")) 

			if cmd == quit:
				stop_server()
				my_log("Exit program",t0)

			elif cmd == info:
				my_log("{} TCP connections, UDP connection of {}Mbps".format(len(tcp_connections),udp_bandwidth),t0)

			elif create_tcp_flows.match(cmd) is not None:
				m = create_tcp_flows.match(cmd)
				num = int(m.group(1))
				if num>0:
					for i in range(num):
						tcp_connections.append(launch_bg(
							"iperf -t 10000000 -p{} -c {}".format(tcp_server_port,server_ip)))
					my_log("Start {} TCP connection ({} active)".format(num,len(tcp_connections)),t0)

			elif kill_tcp_flows.match(cmd) is not None:
				m = kill_tcp_flows.match(cmd)
				num = int(m.group(1))
				if num>0:
					killed = 0
					for i in range(num):
						if len(tcp_connections)>0:
							tcp_connections[-1].kill()
							tcp_connections.pop()
							killed += 1
					my_log("Kill {} TCP connection ({} active)".format(killed,len(tcp_connections)),t0)


			elif create_udp_flow.match(cmd) is not None:
				m = create_udp_flow.match(cmd)
				num = int(m.group(1))
				if num>0:
					"""
					Kill the previous connection
					"""
					if udp_connection is not None:
						udp_connection.kill()
						udp_connection = None
					"""
					Start the new connection with the new rate
					"""
					udp_connection = launch_bg(
						"iperf -t 10000000 -p{} -c {} -u -b {}m".format(udp_server_port, server_ip, num))
					my_log("Start UDP connection - {} Mbit/s".format(num),t0)
					udp_bandwidth = num

			elif cmd == kill_tcp:
				if len(tcp_connections)>0:
					killed = 0
					for i in range(0,len(tcp_connections)):
						tcp_connections[-1].kill()
						tcp_connections.pop()
						killed += 1
					my_log("Kill {} TCP connection ({} active)".format(killed,len(tcp_connections)),t0)


			elif cmd == kill_udp:

				if udp_connection is not None:
					udp_connection.kill()
					udp_connection = None
					my_log("Kill UDP connection",t0)
					udp_bandwidth = 0

			elif cmd == kill_all:
				killall("iperf")
				tcp_connections = [] 
				udp_connection = None
				udp_bandwidth = 0
				my_log("Kill all connections",t0)

			elif cmd == save:
				plt.savefig("plot-"+str(time.time())+".pdf", format="PDF")

			elif cmd == p:
				if pause.is_set():
					pause.clear()
				else:
					pause.set()

			else:
				my_log("Invalid command",t0)

	except (KeyboardInterrupt):
		stop_server()
	finally:
		print "Keyboard listener terminated"


		

def execute_matplotlib(data, w_size):
	
	x_lim_left = 0 
	x_lim_right = 1
	wtw = 2 # white time window
	wus = 1.1 # white upper space
	lines = {} # lines to plot
	ax = {} # axes or subplots
	
	fig = plt.figure(1, figsize=w_size)
	plt.ion()

	# format bitrates on y axis
	mkfunc = lambda x, pos: '%1.1fM' % (x*1e-6) if x>=1e6 else '%1.1fK' % (x*1e-3) if x>=1e3 else '%1.1f' % x
	mkformatter = matplotlib.ticker.FuncFormatter(mkfunc)

	"""
	Subplots initialization
	"""
	for key in data:
		ax[key] = fig.add_subplot(data[key]["position"])
		ax[key].set_ylabel(data[key]["ylabel"])
		ax[key].set_title(data[key]["title"])
		if "xlabel" in data[key]:
			ax[key].set_xlabel(data[key]["xlabel"])
		ax[key].grid()
	
	ax["txrate"].yaxis.set_major_formatter(mkformatter)

	fig.subplots_adjust(
		left=0.08, 
		bottom=0.08, 
		top=0.94, 
		right=0.94)

	plt.show()

	while not stop.is_set():

		time.sleep(IPERF_REPORT_INTERVAL)

		if pause.is_set():
			continue

		now = int(time.time()-t0)

		
		"""
		Update axis and do reset
		"""
		x_lim_right = int(now + wtw)

		"""
		If x_lim_right exceed MAX_TIME_WINDOW,
		start to slide:
			- update x_lim_left
			- delete out-of-graph data
		"""
		if x_lim_right > MAX_TIME_WINDOW:
			x_lim_left = x_lim_right - MAX_TIME_WINDOW 


		"""
		Initialize lines
		"""
		for key in ["txrate", "rtt"]:
			lines[key] = {}
			for src in data[key]["samples"]:
				lines[key][src], = ax[key].plot([],[], label=key, color="black")

		"""
		Update lines
		"""
		with sem_data:
			for key in data:

				"""
				Update axis
				"""
				ax[key].set_ylim(0, data[key]["max"] * wus)
				ax[key].set_xlim(x_lim_left, x_lim_right)

				for src in data[key]["samples"]:

					"""
					Add new lines
					"""
					if key not in lines:
						lines[key] = {}

					if src not in lines[key]:
						lines[key][src], = ax[key].plot([], [], label=src)

					"""
					Update lines
					"""
					lines[key][src].set_data(
						data[key]["samples"][src]["t"],
						data[key]["samples"][src]["val"]
						)

			fig.canvas.draw()

	plt.close()
	print "Matplotlib terminated"



# """
# if the max was taken outside the visualized graph,
# update max looking all the array
# """
# def update_maxs(data, x_lim_left):
# 	for key in data:
# 		if data[key]["t_max"]<x_lim_left:
# 			# one line data
# 			if "t" in data[key]["samples"]:
# 				if len(data[key]["samples"]["val"])>0:
# 					new_max = max(data[key]["samples"]["val"])
# 					pos_max = (data[key]["samples"]["val"]).index(new_max)
# 					data[key]["max"] = new_max
# 					data[key]["t_max"] = data[key]["samples"]["t"][pos_max]
# 			# only for cwnd sum
# 			else:
# 				if len(data[key]["samples"]["SUM"]["val"])>0:
# 					new_max = max(data[key]["samples"]["SUM"]["val"])
# 					pos_max = (data[key]["samples"]["SUM"]["val"]).index(new_max)
# 					data[key]["max"] = new_max
# 					data[key]["t_max"] = data[key]["samples"]["SUM"]["t"][pos_max]

def set_data(intf, server_ip):
	data = {
		"txrate" : {
			"title" 	: "Transmission Rate",
			"ylabel" 	: "bit/s",
			"position" 	: 311,
			"max" 		: 1,
			"t_max"		: 1,
			"samples" 	: { intf: {"t":[], "val":[]} }
			
		},
		"cwnd" : {
			"title" 	: "Congestion Window",
			"ylabel" 	: "Byte",
			"position" 	: 312,
			"max" 		: 1,
			"t_max"		: 1,
			"min"		: 1000000, 
			"samples" 	: {}, # dict of "src" = {"t":[], "val":[]}
		},
		"rtt" : {
			"title" 	: "RTT",
			"ylabel" 	: "ms",
			"xlabel"	: "time [s]",
			"position" 	: 313,
			"max" 		: 0.0005,
			"t_max"		: 1,
			"samples" 	: { server_ip: {"t":[], "val":[]} }		
		}
	}
	return data


# """
# Delete all samples before x_lim_left
# """
# def drop_data(data, x_lim_left):
# 	for key in data:
# 		# one line data
# 		if "t" in data[key]["samples"]:
# 			index = first_index_geq(data[key]["samples"]["t"],x_lim_left)
# 			del (data[key]["samples"]["t"])[:index]
# 			del (data[key]["samples"]["val"])[:index]
# 		else:
# 			for src in data[key]["samples"]:
# 				index = first_index_geq(data[key]["samples"][src]["t"],x_lim_left)
# 				del data[key]["samples"][src]["t"][:index]
# 				del data[key]["samples"][src]["val"][:index]
			


def stop_server():
	print "Stopping the server..."
	stop.set()

def run_program(intf, server_ip, tcp_port, udp_port, window_size):
	pause.clear() # clear the pause plot event
	stop.clear() # clear the stop event
	data = set_data(intf, server_ip) # initialize the data structure
	insert_tcp_probe_module(tcp_port)
	global t0 # use a single global initial time stamp
	t0 = time.time() # t0 is now

	#--------------Start all threads here---------------------

	threads = {
		"txrate"	: threading.Thread(target=bwm_ng_thread, args=(data["txrate"], intf)),
		"cwnd"		: threading.Thread(target=tcp_probe_thread, args=(data["cwnd"],)),
		"rtt" 		: threading.Thread(target=ping_thread, args=(data["rtt"], server_ip)),		
		"keyboard"  : threading.Thread(target=keyboard_listener_thread, args=(server_ip, tcp_port, udp_port))	
	}

	for t in threads:
		threads[t].start()
	
	execute_matplotlib(data, window_size)

	# wait until the end of the test
	try:
		while not stop.is_set():
			time.sleep(2)
	except (KeyboardInterrupt):
		print "Server interrupted by the user..."
		stop_server()
	finally:
		print "Server terminated!"
		programs = ["ping", "cat", "iperf", "bwm-ng"]
		for prog in programs:
			killall(prog)
		subprocess.call("sudo modprobe -r tcp_probe", shell=True) 
		




parser = argparse.ArgumentParser(description='Plot outgoing iPerf connections')

parser.add_argument('-i', dest='intf', nargs=1, default='wlp8s0',
	help='The network interface name transmitting data')

parser.add_argument('-c', dest='server_ip', nargs=1, default="192.168.1.12", 
	help='Server IP address')

parser.add_argument('-t', dest='tcp_port', nargs=1, default=5001, type=int, 
	help='TCP server port')

parser.add_argument('-u', dest='udp_port', nargs=1, default=5201, type=int, 
	help='UDP server port')

parser.add_argument('-w', dest='window_size', nargs=2, default=[11,8], type=int, 
	help='Width and height of the window [inch]')

args = parser.parse_args()

run_program(args.intf, args.server_ip, args.tcp_port, args.udp_port, args.window_size)