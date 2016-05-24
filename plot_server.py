#!/usr/bin/python
import sys, time, getopt, threading, matplotlib, inspect
import argparse
import matplotlib.pyplot as plt
import numpy as np
from threading import Timer
from mylib import *
from numpy import ones,vstack
from numpy.linalg import lstsq
from scipy import interpolate

"""
This program executes an iperf TCP, UDP server and
shows the bandwidth used by each source IP.
Since it aggregate flows, an iperf client must create at least 
2 connection to be displayed.  
"""

sem_data  = threading.Semaphore(1)    # semaphore for operations on data
stop        = threading.Event() # event to stop every thread
pause       = threading.Event() # event to pause the visualizations
global t0   # unix timestamp of the reference instant
clients_id = [] # list of all couples (ip-port) that identify an user
DEATH_TOLERANCE = 2*IPERF_REPORT_INTERVAL # time with no reports after which a user is considered dead
T = IPERF_REPORT_INTERVAL * 0.8 # reports in [t-T,t+T] are burned
max_time_window = 60 # The graph keeps expanding until max_time_window [seconds], then data and graph are reset
SMOOTH_WINDOW = 16 # number of samples to be smoothed
DENSITY_LINSPACE = 4 # resampling frequency
SMOOTH_WINDOW = 10 # smoothing window [s]


def stop_server():
	print "Stopping the server..."
	stop.set()

"""
Print the plot legend
"""
def print_legend(subplot, num_flows):
	if num_flows==1:
		title = "1 user"
	else:
		title = "{} users".format(num_flows)

	subplot.legend(
		bbox_to_anchor=(1.03, 1), 
		loc=2,
		borderaxespad=0.,
		title=title)

"""
Returns a smoothed list
"""
def smooth(list_x, smooth_window):
	if smooth_window < 3:
		return list_x

	if len(list_x) < smooth_window:
		return list_x
	x = np.array(list_x)
	s = np.r_[x[smooth_window-1:0:-1],x,x[-1:- smooth_window:-1]]
	w = eval('np.'+ 'hanning' +'(smooth_window)')
	y = np.convolve(w / w.sum(), s , mode='valid')
	sm =  list(y[(smooth_window / 2 - 1):-(smooth_window / 2)])
	len_sm = len(sm)
	len_x = len(list_x)
	if len_sm == len_x:
		return sm
	if len_sm<len_x:
		sm += [0] * (len_x - len_sm)
		return sm
	else:
		return sm[:len_x]


#------------------------ PARSING --------------------------------

"""
Return true if the TCP line is valid, false otherwise
"""
def is_valid_iperf_tcp_line(cols, report_interval):
	if len(cols) < 8:
		return False
	# Summation line
	if (cols[2], cols[4], cols[5][0]) != ("0","0", "-"):
		return False
	# Invalid report interval - end of transmission reports
	intvs = cols[6].split("-")
	intv0 = float(intvs[0])
	intv1 = float(intvs[1])
	if intv1 - intv0 != float(report_interval):
		return False
	return True

"""
Return true if the UDP line is valid, false otherwise
"""
def is_valid_iperf_udp_line(cols, report_interval):
	if len(cols) != 14 or int(cols[13]) != 0:
		return False
	# Invalid report interval - end of transmission reports
	intvs = cols[6].split("-")
	intv0 = float(intvs[0])
	intv1 = float(intvs[1])
	if intv1 - intv0 != float(report_interval):
		return False
	return True

#------------------------------ SUM OF FLOWS -------------------------------------#

"""
Find the first index where time >= t
data is a time list
if it does not exist, return -1
"""
def first_index_geq(data, t):
	index = -1
	for i in range(len(data)):
		if data[i] >= t:
			index = i
			break
	return index

"""
Find to which flows belong t and its index
data is data[uid]
"""
def find_timestamp(data,t):
	prot = "tcp"
	index = -1
	try:
		index = data[prot]["t"].index(t)
	except ValueError:
		prot = "udp"
		index = data[prot]["t"].index(t)    
	finally:
		return [prot,index]

"""
Delete samples around t and insert the new sample in the right position
data is data[uid][prot]
"""
def fire(data,t):
	# update the series of the protocol
	begin = first_index_geq(data["t"],t-T) 
	end = first_index_geq(data["t"],t+T)
	if end-begin>0:
		del(data["t"][begin:end])
		del(data["val"][begin:end])

"""
insert t,val in the correct position of data
data is data[uid][x]
"""
def insert_value(data,t,val):
	index = first_index_geq(data["t"],t)
	if index == -1:
		data["t"].append(t)
		data["val"].append(val)
	else:
		data["t"].insert(index,t)
		data["val"].insert(index,val)


def get_other(prot):
	if prot == "tcp":
		return "udp"
	return "tcp"

"""
Interpolation
return the value that link values of data around t in t
return 0 if a line cannot be traced
data is data[uid][prot]
"""
def line_around_t(data,t):
	i2 = first_index_geq(data["t"],t)
	if i2>0:
		x_coords = (data["t"][i2-1],data["t"][i2])
		y_coords = (data["val"][i2-1],data["val"][i2])
		A = vstack([x_coords,ones(len(x_coords))]).T
		m, c = lstsq(A, y_coords)[0]
		return (m*t)+c
	return -1

"""
Append a 0 sample if last received sample is too far
return True if a dead is declared, False otherwise
Data is data[uid][prot]
"""
def update_death_flows(data,t):
	if len(data["t"])>0:
		last_t = data["t"][-1]
		if t - last_t >= DEATH_TOLERANCE:
			data["t"].append(last_t + IPERF_REPORT_INTERVAL)
			data["val"].append(0)
			return True
	return False



def update_sum(data,t,val,uid,prot,singles):
	# print "Begin with {}".format(data[prot]["t"])
	other = get_other(prot)
	# delete samples around t because they are fake 
	fire(data[prot],t)  
	# insert the new poit
	insert_value(data[prot],t,val)
	#add the new point in total
	insert_value(data["total"],t,val)
	# declare as singles all totals in the burned interval
	# print "---------------------------"
	# print "Start update with {} singles".format(len(singles))
	declare_as_singles(data["total"]["t"],t-T,t+T,singles)
	# print "After singles in fire, {}".format(len(singles))
	# see if the other flow is dead and declare it
	if update_death_flows(data[other],t):
		# if something changed, declare these points as singles
		declare_as_singles(data["total"]["t"],data[other]["t"][-2],data[other]["t"][-1],singles)
	# update singles: sort and delete samples too old
	singles = update_singles(singles)
	# print "After update, {}".format(len(singles))
	# try to solve all singles
	singles = solve_singles(data,singles)
	# print "After solution, {}".format(len(singles))

	return singles
	#print "End with {}".format(data[prot]["t"])

#------------------------------ SINGLES -------------------------------------#

# Declare as singles the totals between [t1,t2]
# data is data[uid][total]["t"]
def declare_as_singles(data,t1,t2,singles):
	# print len(singles),
	begin = first_index_geq(data,t1) 
	end = first_index_geq(data,t2)
	if begin<0:
		print ""
		return
	if end<0:
		end = len(data)
	for i in range(begin,end):
		singles.append(data[i])
	# print len(singles)

# Order singles and delete old singles
def update_singles(singles):
	# list(set()) eliminate duplicates
	s = sorted(list(set(singles)))
	last_t = s[-1]
	first_valid = first_index_geq(s, last_t- max_time_window)
	if first_valid>0:
		# print "delete old singles {},".format(len(singles)),
		del s[:first_valid]
		# print len(singles)
	return s

# Solve the list of singles
# data is data[uid]
def solve_singles(data,singles):
	solved_singles = []
	for single in singles:
		prot, index = find_timestamp(data,single)
		val1 = data[prot]["val"][index]
		val2 = line_around_t(data[get_other(prot)],single)

		# we have to be sure that already exists
		index_in_total = data["total"]["t"].index(single)
		data["total"]["val"][index_in_total] = val1+val2
		if val2>0:
			solved_singles.append(single)

	s = list(singles)
	for single in solved_singles:
		del s[s.index(single)]
	return s
	
#-------------------------- DATA MANAGEMENT -------------------------------

def check_number_of_users(data, expected_users):
	if update_alive_flows(data) < expected_users:
		print "Less users than expected, aborting..."
		stop_server()


# Count the number of active flows
def update_alive_flows(data):
	num = 0
	with sem_data:
		now = time.time()-t0
		del clients_id[:]
		for src in data:
			if src != "SUM":
				for key in data[src]:
					if (len(data[src][key]["t"])>0 and 
						abs(now - data[src][key]["t"][-1]) <= DEATH_TOLERANCE*4 and 
						data[src][key]["val"][-1]>0):
						clients_id.append(src)
						break
		num = len(clients_id)
	return num

# initialize the data structure
# data = {
# 		"tcp"       : {"t":[], "val":[]},
# 		"udp"       : {"t":[], "val":[]},
# 		"total"     : {"t":[], "val":[]}
# 	}
# the user "SUM" has only the dict "total"
def set_data():
	data = {}
	data["SUM"] = {"total": {"t":[], "val":[]}}
	return data

# create the dict for a new client
def new_client_data():
	data = {
		"tcp"       : {"t":[], "val":[]},
		"udp"       : {"t":[], "val":[]},
		"total"     : {"t":[], "val":[]},
	}
	return data


#------------------------------ THREADS -------------------------------------#

def iperf_tcp_thread(data, port,singles):
	print "iPerf TCP server listening on port {}".format(port)
	report_interval = IPERF_REPORT_INTERVAL
	cmd = "iperf -s -i{} -fk -yC -p{}".format(report_interval, port)

	tzeros = {} # first timestamp of each user
	
	for line in runPexpect(cmd):
		if stop.is_set():
			break
		"""
		example line: 
		0              1             2    3             4     5    6     7          8
		20150803124132,10.100.13.214,5001,10.100.13.162,56695,4,0.0-17.4,1005453312,463275664

		0: timestamp
		1: server_ip
		2: server_port
		3: client_ip
		4: client_port
		5: connection id (for iperf)
		6: time-interval
		7: bytes transferred in the interval
		8: rate in the interval
		"""
		cols = line.split(",")

		if not is_valid_iperf_tcp_line(cols,report_interval):
			continue

		ip, rate_bps = str(cols[3]), float(cols[8])
		uid = ip#"{}-{}".format(ip, port)

		# iperf date is formatted, get the corresponding unix timestamp
		stamp = time.time()-t0

		val_tcp = rate_bps #float(num_bytes * 8) / float(report_interval)

		with sem_data:
			if uid not in data:
				data[uid] = new_client_data()
			
			intvs = cols[6].split("-")
			intv0 = float(intvs[0])
			intv1 = float(intvs[1])

			if intv0 == 0.0 or uid not in tzeros:
				tzeros[uid] = stamp - report_interval

			stamp = tzeros[uid] + intv1

			if uid not in singles:
				singles[uid] = []
			singles[uid] = update_sum(data[uid],t=stamp,val=val_tcp,uid=uid, prot="tcp", singles=singles[uid])

	print "iPerf TCP server (port {}) terminated".format(port)


def iperf_udp_thread(data, port, singles):
	print "iPerf UDP server listening on port {}".format(port)
	report_interval = IPERF_REPORT_INTERVAL
	cmd = "iperf -s -i{} -fk -yC -u -p{}".format(report_interval, port)
	tzeros = {}
	for line in runPexpect(cmd):
		if stop.is_set():
			break
		"""
		example line: (len=14)
		0              1             2    3             4     5    6     7       8       9     10 11  12    13
		20150803222713,192.168.100.4,5002,192.168.100.2,36823,3, 5.0-6.0,24990,  199920, 0.011,0, 17, 0.000,0
		20150804101346,10.100.13.162,5002,10.100.13.214,47833,11,4.0-5.0,1249500,9996000,0.025,0, 850,0.000,0

		0:  timestamp
		1:  server_ip
		2:  server_port
		3:  client_ip
		4:  client_port
		5:  connection-id (for iperf)
		6:  time-interval
		7:  bytes ?
		8:  bandwidth ?
		9:  jitter ?
		10: lost datagrams ?
		11: total datagrams ?
		12: lost percentage ?
		13: out-of-order diagrams ?
		"""

		cols = line.split(",")
		if not is_valid_iperf_udp_line(cols,report_interval):
			continue

		ip, num_bytes = str(cols[3]), int(cols[7])
		uid = ip #"{}-{}".format(ip, port)

		# iperf date is formatted, get the corresponding unix timestamp
		stamp = time.time()-t0

		val_udp = float(num_bytes * 8) / float(report_interval)

		with sem_data:
			if uid not in data:
				data[uid] = new_client_data()


			intvs = cols[6].split("-")
			intv0 = float(intvs[0])
			intv1 = float(intvs[1])
			
			"""
			UDP is connectionless so the first sample may be lost
			We take as t0 the first datagram effectively arrived
			"""         
			if intv0 == 0.0 or uid not in tzeros:
				tzeros[uid] = stamp - report_interval

			stamp = tzeros[uid] + intv1

			if uid not in singles:
				singles[uid] = []
			singles[uid] = update_sum(data[uid],t=stamp,val=val_udp,uid=uid, prot="udp", singles=singles[uid])

	print "iPerf UDP server (port {}) terminated".format(port)


def bwm_ng_thread(data, interface):
	print "bwm-ng thread started, measuring {} input traffic".formar(interface)
	cmd = "bwm-ng -u bits -T rate -t 1000 -I {} -d 0 -c 0 -o csv".format(interface)

	for line in runPexpect(cmd):
		if stop.is_set():
			break
		"""
		example line: 
		0          1    2       3         4         5     6   7     8      9      10 11 12  13  14 15
		1437515226;eth0;1620.00;123595.00;125215.00;24719;324;25.00;110.00;135.00;22;5;0.00;0.00;0;0
		1437515226;total;1620.00;123595.00;125215.00;24719;324;25.00;110.00;135.00;22;5;0.00;0.00;0;0

		0: unix timestamp   *
		1: interface
		2: bytes_out/s      *
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

		bwm t0:1437516400.0
		png t0:1437517839.21

		ping stamp:0.200218200684
		bwm stamp: 1.0

		The first (like) 10 timestamps comes at 1ms distance, 
		the others every 1sec

		Also if passing the -u bits option, rate reamins is in byte/s

		"""
		# Parsing
		if line.find("total") == -1 : #only reports, not the total
			cols = line.split(";")
			stamp = int(cols[0])-t0 
			rate = float(cols[3])*8 # conversion byte/s --> bit/s

			with sem_data:
				data["t"].append(stamp)
				data["val"].append(rate)

	print "bwm-ng thread terminated"


def execute_matplotlib(data):

	x_lim_left = 0 
	x_lim_right = 1
	wtw = 2 # white time window
	wus = 1.1 # white upper space
	lines = {} # lines to plot
	ax = {} # axes or subplots
	
	fig = plt.figure(1, figsize=(9,6))
	plt.ion()
	plt.show(block=False)

	subplots = {
		"tcp-udp" : {
			"position"  : 211,
			"title"     : "Per-user TCP/UDP raw rate",
			"ylabel"    : "bit-rate [bit/s]"
		},
		"total" : {
			"position"  : 212,
			"title"     : "Per-user smoothed rate ({}s window)".format((SMOOTH_WINDOW*IPERF_REPORT_INTERVAL)/DENSITY_LINSPACE),
			"xlabel"	: "time [s]",
			"ylabel"    : "bit-rate [bit/s]"
		}
	}

	# format bitrates on y axis
	mkfunc = lambda x, pos: '%1.1fM' % (x*1e-6) if x>=1e6 else '%1.1fK' % (x*1e-3) if x>=1e3 else '%1.1f' % x
	mkformatter = matplotlib.ticker.FuncFormatter(mkfunc)
	

	for key in subplots:
		ax[key] = fig.add_subplot(subplots[key]["position"])
		ax[key].set_ylabel(subplots[key]["ylabel"])
		if "xlabel" in subplots[key]:
			ax[key].set_xlabel(subplots[key]["xlabel"])
		ax[key].set_title(subplots[key]["title"])
		ax[key].grid()
		ax[key].yaxis.set_major_formatter(mkformatter)

	fig.subplots_adjust(
		left=0.08, 
		bottom=0.08, 
		top=0.94, 
		right=0.80)

	lines["SUM"] = {}
	lines["SUM"]["total"], = ax["tcp-udp"].plot([],[], label="SUM", color="black")
	print_legend(ax["tcp-udp"],0)
   
	# ------------------------------- MAIN PLOT CICLE -----------------------------
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
		If x_lim_right exceed max_time_window,
		start to slide:
			- update x_lim_left
			- delete out-of-graph data
		"""
		if x_lim_right > max_time_window:
			x_lim_left = x_lim_right - max_time_window 
	
		"""
		Update lines
		"""
		with sem_data:
			"""
			Dinamically set the graph height
			"""
			for key in subplots:
				if x_lim_right > max_time_window:
					if key=="tcp-udp" and len(data["SUM"]["total"]["val"])>x_lim_left:
						max_y = np.max(data["SUM"]["total"]["val"][x_lim_left:])
					else:
						max_y = 1
						for uid in data:
							if uid!="SUM" and len(data[uid]["total"]["val"])>x_lim_left:
								new_max = np.max(data[uid]["total"]["val"][x_lim_left:])
								if new_max>max_y:
									max_y = new_max
				else:
					if key=="tcp-udp" and len(data["SUM"]["total"]["val"])>0:
						max_y = np.max(data["SUM"]["total"]["val"])
					else:
						max_y = 1
						for uid in data:
							if uid!="SUM" and len(data[uid]["total"]["val"])>0:
								new_max = np.max(data[uid]["total"]["val"])
								if new_max>max_y:
									max_y = new_max

				ax[key].set_ylim(0, max(1,max_y)*wus)  
				ax[key].set_xlim(x_lim_left, x_lim_right)  

			for src in data:
				if src!= "SUM" and src not in lines:
					lines[src]={}
					src_color = ""
					for key in sorted(data[src]):
						if key == "tcp":
							lines[src][key], = ax["tcp-udp"].plot([],[], label=src)
							src_color = lines[src][key].get_color()
						elif key == "total":
							lines[src][key], = ax["total"].plot([],[], color = src_color, antialiased = True)
						elif key == "udp":
							lines[src][key], = ax["tcp-udp"].plot([],[], color = src_color, linestyle = "--")

			
				for key in data[src]:
					first_index = max(0,first_index_geq(data[src][key]["t"],x_lim_left)-2)
					last_index = max(0,len(data[src][key]["t"])-1)
					x = list(data[src][key]["t"][first_index:last_index])
					y = list(data[src][key]["val"][first_index:last_index])

					if src!="SUM" and key=="total" and len(x)>(SMOOTH_WINDOW/DENSITY_LINSPACE)+1:
						f = interpolate.interp1d(x,y)
						new_x = np.linspace(min(x),max(x), (x_lim_right - x_lim_left)*DENSITY_LINSPACE )
						new_y = smooth(f(new_x), window_len=SMOOTH_WINDOW)
						lines[src][key].set_data(new_x,new_y)
					else:							
						lines[src][key].set_data(x,y)
		
		print_legend(ax["tcp-udp"],update_alive_flows(data))
		fig.canvas.draw()       

	plt.close()
	print "Matplotlib terminated"


def keyboard_listener_thread(do_visualize):
	help_string = """Keyboard listener started\n
	Commands:\n\
	q: Quit the program\n\
	s: Save a pdf screenshot of the plot\n\
	p: Pause the plot"""

	print help_string

	quit_key = "q"
	save_key = "s"
	pause_key ="p"   
	input_key = ""
	try:
		while input_key != quit_key:  
			input_key = str(raw_input("Command: ")) 
			if input_key == quit_key:
				stop_server()
			elif input_key == pause_key and do_visualize:
				if pause.is_set():
					pause.clear()
				else:
					pause.set()
			elif input_key == save_key and do_visualize:
				plt.savefig('plot-{}.pdf'.format(time.time()), format="PDF")
			elif input_key != quit_key:
				print "Invalid command key"
	except (KeyboardInterrupt):
		stop_server()
	finally:
		print "Keyboard listener terminated"


#--------------------- MAIN PROGRAM -----------------------------


def run_server(intf, tcp_ports, udp_ports, duration, 
	do_visualize, do_check, expected_users, check_t):

	pause.clear() # clear the pause plot event
	stop.clear() # clear the stop event
	data = set_data() # initialize the data structure
	singles = {} # timestamps of sums executed without an element for each uid
	threads = {} # dict of threads	
	killall("iperf") # Delete any previous process
	killall("bwm-ng") # Delete any previous process
	global t0 # use a single global initial time stamp
	t0 = time.time() # t0 is now

	threads["bwm-ng"] = threading.Thread(
		target=bwm_ng_thread, 
		args=(data["SUM"]["total"],intf))

	for tcp_port in tcp_ports:
		threads["iperf_tcp_"+str(tcp_port)] = threading.Thread(
			target=iperf_tcp_thread, 
			args=(data,tcp_port,singles))

	for udp_port in udp_ports:
		threads["iperf_udp_"+str(udp_port)] = threading.Thread(
			target=iperf_udp_thread, 
			args=(data,udp_port,singles))

	stop_timer = Timer(duration, stop_server)
	if duration > 0:		
		stop_timer.start()			
	else:
		threads["keyboard"] = threading.Thread(
			target=keyboard_listener_thread,
			args=(do_visualize,))

	if do_check and expected_users > 0 and check_t > 0:
		check_timer = Timer(check_t, check_number_of_users, args=(data, expected_users))
		check_timer.start()
	
	# start iperf and keyboard threads
	for t in threads:
		threads[t].start()

	# start the plot
	if do_visualize:
		execute_matplotlib(data)

	# wait until the end of the test
	try:
		print "Running server processes..."
		while not stop.is_set():
			time.sleep(2)
	except (KeyboardInterrupt):
		print "Server interrupted by the user..."
		stop_server()
		data = None
	finally:
		print "Server terminated!"
		stop_timer.cancel()
		killall("iperf")
		killall("bwm-ng")
		return data
	


parser = argparse.ArgumentParser(description='Plot incoming iPerf rates')

parser.add_argument('-i', dest='intf', nargs=1, default='eth0',
	help='The network interface name receiving data')

parser.add_argument('-t', dest='tcp_ports', nargs='+', default=[5001], type=int, 
	help='List of listening TCP ports')

parser.add_argument('-u', dest='udp_ports', nargs='+', default=[5201], type=int, 
	help='List of listening UDP ports')

parser.add_argument('-d', dest='duration', nargs=1, default=-1, type=int, 
	help='Duration of the test [seconds]. Default infinite')

parser.add_argument('--no-plot', dest='do_visualize', action='store_false',
	help='Do not show the plot')
parser.set_defaults(do_visualize=True)

parser.add_argument('--do-check', dest='do_check', action='store_true',
	help='Check the number of active users at a given instant')
parser.set_defaults(do_check=False)

parser.add_argument('-c', dest='check_t', nargs=1, default=1, type=int, 
	help='Instant to check the number of active users')

parser.add_argument('-e', dest='expected_users', nargs=1, default=1, type=int, 
	help='Number of expected active users at the check time')

args = parser.parse_args()

run_server(args.intf, args.tcp_ports, args.udp_ports, args.duration, 
	args.do_visualize, args.do_check, args.expected_users, args.check_t)
