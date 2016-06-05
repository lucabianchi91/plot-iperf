#!/usr/bin/python

import sys, time, datetime, getopt, subprocess, os, threading, pexpect, tty, termios, re
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.font_manager import FontProperties
from mylib import *

UPDATE_INTERVAL = 1					
sem_data 		= threading.Semaphore(1) 	# semaphore for operations on data
stop 			= threading.Event() 		# set if the program is running
pause			= threading.Event()			# set if the graph is in pause
global t0 	# unix timestamp of the reference instant

"""
The graph keeps expanding until max_time_window [seconds],
then starts to slide and delete out-of-graph data
"""
max_time_window = 60

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
Kill all processes
"""
def clean_system():
	programs = ["ping", "cat", "iperf", "bwm-ng"]
	for prog in programs:
		killall(prog)
	subprocess.call("sudo modprobe -r tcp_probe", shell=True) 


"""
Thread that execute, parse and write ping (rtt measure)
"""
def ping_thread(data):
	for line in runPexpect(data["command"]):
		if stop.is_set():
			break
		"""
		example line: 
		1                   2  3     4    5              6           7      8
		[1437417582.711328] 64 bytes from 10.100.13.214: icmp_seq=21 ttl=64 time=0.104 ms
		"""
		cols = line.split(" ")
		if len(cols)==9 and line[0] == "[": #only reports, not the final average			
			stamp = float((cols[0])[1:len(cols[0])-1])-t0
			cols2 = cols[7].split("=")
			if len(cols2)==2:
				rtt = float(cols2[1])
				with sem_data:
					data["samples"]["t"].append(stamp)
					data["samples"]["val"].append(rtt)
					if rtt > data["max"]:
						data["max"] = rtt
						data["t_max"] = stamp


"""
Thread that execute, parse and write bwm-ng (bandwidth measure)
"""
def bwm_ng_thread(data):
	for line in runPexpect(data["command"]):
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
		if line.find("total") == -1 : # only reports, not the total
			cols = line.split(";")
			stamp = int(cols[0])-t0 
			rate = float(cols[2])*8 # conversion byte/s --> bit/s
			with sem_data:
				data["samples"]["t"].append(stamp)
				data["samples"]["val"].append(rate)
				if rate > data["max"]:
					data["max"] = rate
					data["t_max"] = stamp

"""
Thread that execute, parse and write tcp-probe (congestion window measure)
"""
def tcp_probe_thread(data):

	# Create the dictionary for the summation of windows
	data["samples"]["SUM"] = {"t":[], "val":[] }

	for line in runPexpect(data["command"]):
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
			if cwnd < data["min"]:
				data["min"] = cwnd

			# if there is a new connection, create its record
			if src not in data["samples"]:
				data["samples"][src]= {"t":[], "val":[]}

			# Save data 	
			data["samples"][src]["t"].append(stamp)
			data["samples"][src]["val"].append(cwnd)

			update_death_flows(data,stamp)
			update_cwnd_sum(data,stamp)

def update_death_flows(data, stamp):
	"""
	if the last sample is too far, 
	we create a null sample just after the last sample.
	The last sample is considered the dead point
	"""
	death_tolerance = 2 # seconds to consider a flow death (to put a zero in his death instant)

	"""
	Apply this only to summation line
	"""
	src = "SUM"
	if len(data["samples"][src]["t"])>1:
		# Take the last istant
		last_t = data["samples"][src]["t"][-1]
		# If it's too far, put a "zero" just after the last sample
		if stamp-last_t > death_tolerance:
			data["samples"][src]["t"].append(last_t+0.001) 	
			data["samples"][src]["val"].append(data["min"])	


def update_cwnd_sum(data, stamp):
	"""
	The value of the sum for this instant is the
	real sample value plus all the previous samples of the other flows
	"""
	sum_tolerance = 0.5 # seconds to consider a flow for the sum of windows
	cwnd_sum = 0
	# For all sources
	for src in data["samples"]: 
		# if the source is not the sum and the last sample is close enough
		if(len(data["samples"][src]["t"])>0 
				and src!="SUM" 
				and stamp-data["samples"][src]["t"][-1]<sum_tolerance):
			# consider the sample in the summation
			cwnd_sum += data["samples"][src]["val"][-1]

	data["samples"]["SUM"]["t"].append(stamp) # temporal axis with all values	
	data["samples"]["SUM"]["val"].append(cwnd_sum)
	
	if cwnd_sum > data["max"]:
		data["max"] = cwnd_sum
		data["t_max"] = stamp


def keyboard_listener(server_ip, tcp_server_port, udp_server_port):
	tcp_connections = [] 
	udp_connection = None
	udp_bandwidth = 0

	"""
	+#t 	--> create # TCP flows
	-#t 	--> kill # TCP flows 	
	+#u 	--> create 1 UDP flows with rate #mbps	 
	kt 		--> kill all TCP flows 
	ku 		--> kill UDP flow
	ka 		--> kill all flows
	q 		--> quit the program 
	"""

	create_tcp_flows 	= re.compile("\+([0-9]{1,4})t")
	kill_tcp_flows 		= re.compile("\-([0-9]{1,4})t")
	create_udp_flow 	= re.compile("\+([0-9]{1,4})u")

	kill_tcp 	= "kt"
	kill_udp 	= "ku"
	kill_all 	= "ka"
	quit 		= "q"
	info 		= "i"
	save 		= "s"
	p		= "p"

	cmd = ""
	while cmd != quit:
		cmd = str(raw_input("Command: ")) 

		if cmd == quit:
			stop.set()
			my_log("Exit program",t0)

		elif cmd == info:
			my_log("{} TCP connections, UDP connection of {}Mbps".format(len(tcp_connections),udp_bandwidth),t0)

		elif create_tcp_flows.match(cmd) is not None:
			m = create_tcp_flows.match(cmd)
			num = int(m.group(1))
			if num>0:
				for i in range(num):
					tcp_connections.append(launch_bg("iperf -t 10000000 -p{} -c {}".format(tcp_server_port,server_ip)))
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
				udp_connection = launch_bg("iperf -t 10000000 -p{} -c {} -u -b {}m".format(udp_server_port, server_ip, num))
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
			subprocess.call("sudo killall iperf", shell=True) 
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

def matplotlib_thread(data):
	
	"""
	Parameters
	"""
	x_lim_left = 0
	x_lim_right = 0
	wtw = 2 # white time window
	wus = 1.1 # white upper space

	"""
	Variables initialization
	"""
	lines = {} # lines to plot
	ax = {} # subplots
	plt.ion()
	plt.show()
	fig = plt.figure(1, figsize=(14,14))

	"""
	Subplots initialization
	"""
	for key in data:
		ax[key] = fig.add_subplot(data[key]["position"])
		ax[key].set_ylabel(data[key]["ylabel"])
		ax[key].set_title(data[key]["title"])
		ax[key].grid()

		# if samples is a leaf
		if "t" in data[key]["samples"]:
			lines[key], = ax[key].plot([],[],color="black")
		else: 
			lines[key] = {}
			lines[key]["SUM"], = ax[key].plot([],[],color="black")

	"""
	Plotting loop
	"""
	while not stop.is_set():

		time.sleep(UPDATE_INTERVAL)
		if pause.is_set():
			continue

		now = int(time.time()-t0)
		
		"""
		Update axis and do reset
		"""
		x_lim_right = int(now + wtw)

		"""
		If x_lim_right exceed max_time_window,
		start to slide:
			- update x_lim_left
			- delete out-of-graph data
		"""
		if x_lim_right > max_time_window:
			x_lim_left = x_lim_right - max_time_window
			with sem_data:
				drop_data(data,x_lim_left)
				update_maxs(data, x_lim_left)

		"""
		Update lines
		"""
		with sem_data:

			for key in data:

				# Update axis
				ax[key].set_ylim(0, data[key]["max"]*wus)
				ax[key].set_xlim(x_lim_left, x_lim_right)

				# One line data			
				if "t" in data[key]["samples"]:
					lines[key].set_data(data[key]["samples"]["t"],data[key]["samples"]["val"])
				else: # multi-line data
					for src in data[key]["samples"]:
						if src not in lines[key]:
							lines[key][src], = ax[key].plot([],[])
						lines[key][src].set_data(
							data[key]["samples"][src]["t"],
							data[key]["samples"][src]["val"]
							)

			fig.canvas.draw()

	"""
	The user has quitted the program
	"""
	plt.show(block=True) # keep the image on the screen


"""
if the max was taken outside the visualized graph,
update max looking all the array
"""
def update_maxs(data, x_lim_left):
	for key in data:
		if data[key]["t_max"]<x_lim_left:
			# one line data
			if "t" in data[key]["samples"]:
				if len(data[key]["samples"]["val"])>0:
					new_max = max(data[key]["samples"]["val"])
					pos_max = (data[key]["samples"]["val"]).index(new_max)
					data[key]["max"] = new_max
					data[key]["t_max"] = data[key]["samples"]["t"][pos_max]
			# only for cwnd sum
			else:
				if len(data[key]["samples"]["SUM"]["val"])>0:
					new_max = max(data[key]["samples"]["SUM"]["val"])
					pos_max = (data[key]["samples"]["SUM"]["val"]).index(new_max)
					data[key]["max"] = new_max
					data[key]["t_max"] = data[key]["samples"]["SUM"]["t"][pos_max]

def new_data(network_interface, server_ip):
	data = {
		"txrate" : {
			"title" 	: "Transmission Rate",
			"ylabel" 	: "bit/s",
			"position" 	: 311,
			"starting_max" : 1,
			"max" 		: 1,
			"t_max"		: 1,
			"command"	: "bwm-ng -u bits -T rate -t {} -I {} -d 0 -c 0 -o csv".format(UPDATE_INTERVAL*1000, network_interface),
			"samples" 	: {"t":[], "val":[]}
			
		},
		"cwnd" : {
			"title" 	: "Congestion Window",
			"ylabel" 	: "Byte",
			"position" 	: 312,
			"starting_max" : 1,
			"max" 		: 1,
			"t_max"		: 1,
			"min"		: 1000000, 
			"command"	: "cat /proc/net/tcpprobe",
			"samples" 	: {}, # "src" = {"t":[], "val":[]}
		},
		"rtt" : {
			"title" 	: "RTT",
			"ylabel" 	: "ms",
			"position" 	: 313,
			"starting_max" : 0.0005,
			"max" 		: 0.0005,
			"t_max"		: 1,
			"command"	: "ping -i {} -D {}".format(UPDATE_INTERVAL, server_ip),
			"samples" 	: { "t":[], "val":[]}			
		}
	}
	return data


"""
Delete all samples before x_lim_left
"""
def drop_data(data, x_lim_left):
	for key in data:
		# one line data
		if "t" in data[key]["samples"]:
			index = find_index(data[key]["samples"]["t"],x_lim_left)
			del (data[key]["samples"]["t"])[:index]
			del (data[key]["samples"]["val"])[:index]
		else:
			for src in data[key]["samples"]:
				index = find_index(data[key]["samples"][src]["t"],x_lim_left)
				del data[key]["samples"][src]["t"][:index]
				del data[key]["samples"][src]["val"][:index]
			

"""
Find the first index where time > x_lim_left
data is time []
"""
def find_index(data, x_lim_left):
	i = 0
	for i in range(0,len(data)-1):
		if data[i] >= x_lim_left:
			break
	return max(0,i)

def run_program(network_interface, server_ip, tcp_server_port, udp_server_port):
	
	stop.clear()
	pause.clear()
	data = new_data(network_interface, server_ip)
	insert_tcp_probe_module(tcp_server_port)
	
	global t0
	t0 = time.time()

	#--------------Start all threads here---------------------

	threads = {
		"txrate"	: threading.Thread(target=bwm_ng_thread, args=(data["txrate"],)),
		"cwnd"		: threading.Thread(target=tcp_probe_thread, args=(data["cwnd"],)),
		"rtt" 		: threading.Thread(target=ping_thread, args=(data["rtt"],)),		
		"matplotlib": threading.Thread(target=matplotlib_thread, args=(data,))		
	}

	"""
	Start all threads and read input on the main thread
	"""
	try:
		for key in threads:
			threads[key].daemon = False
			threads[key].start()

		keyboard_listener(server_ip, tcp_server_port, udp_server_port)
		
	except (KeyboardInterrupt, SystemExit): # executed only in case of exceptions
		print "End"		
	finally: # always executed
		clean_system()
		
def main(argv):

	help_string = "Usage: plot_client.py -c <server-ip>"

	server_ip = ""
	tcp_port = 5001
	udp_port = 5002
	network_interface = "eth0"
	help_string = "Usage: plot_client.py -c <server-ip> -i <interface> -t <tcp-port> -u <udp-port>"
	help_string_2 = "TCP<>UDP!"

	try:
		opts, args = getopt.getopt(argv,"hc:i:t:u:",["server-ip=","interface=","tcp-port=","udp-port="])
	except getopt.GetoptError:
		print help_string
		sys.exit(2)

	for opt, arg in opts:
		if opt == '-h':
			print help_string
			sys.exit()
		elif opt in ("-i", "--interface"):
			network_interface = str(arg)
		elif opt in ("-t", "--tcp-port"):
			tcp_port = int(arg)
		elif opt in ("-u", "--udp-port"):
			udp_port = int(arg)
		elif opt in ("-c", "--server-ip"):
			server_ip = str(arg)

	if server_ip=="":
		print help_string
		sys.exit(2)

	run_program(network_interface, server_ip, tcp_port, udp_port)

if __name__ == "__main__":
   main(sys.argv[1:])
