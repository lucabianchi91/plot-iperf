import os, re, pexpect, subprocess, math, time
import numpy as np

FNULL = open(os.devnull, "w")

# --------------------- REFOX NETWORK IP ADDRESSES ------------------
NETWORK_PREFIX = "192.168.200"
NETWORK_PREFIX_16 = "192.168"
HOST_IDS = [1, 2, 3]
STARTING_IPS = [110, 120, 130] 
SERVER_ID = 4
SWITCH_ID = 98
SERVER_IP = "{}.{}".format(NETWORK_PREFIX, SERVER_ID)
SWITCH_IP = "{}.{}".format(NETWORK_PREFIX, SWITCH_ID)
NUM_VETHS = 10

"""
The maximum number of prio classes is 16
but only 15 are available
"""
MAX_NUM_BANDS = 15 # maximum number of DSCP and queues

# pc1 --->192.168.200.1 ---> [110, 119]
# pc2 --->192.168.200.2 ---> [120, 129]
# pc3 --->192.168.200.3 ---> [130, 139]
ADDRESSES = {}
index_pc = 0
for pc in HOST_IDS:
	pc_ip = "{}.{}".format(NETWORK_PREFIX, pc)
	ADDRESSES[pc_ip] = []
	for i in range(NUM_VETHS):
		addr_id = STARTING_IPS[index_pc] + i
		addr_str = "{}.{}".format(NETWORK_PREFIX, addr_id)
		ADDRESSES[pc_ip].append(addr_str)
	index_pc +=1

# --------------------- MARKERS ------------------
HZ = 250 	# [Hz] frequency of tokens update
MTU = 1500 	# [Byte] maximum transfer unit of the physical layer
UDP_PACKET_SIZE = 1470 		# [Byte] packet size of UDP datagrams genererated by iperf
MAX_PACKET_SIZE = 64 * 1024 	# [Byte] maximum packet size of TCP packets [at 1Gbps]
RTT_NO_NETEM = 0.3 # rtt to be saved if netem is not used = real rtt

NO_MARKERS = "no_markers"
BUCKETS_MARKERS = "buckets_markers"
IPTABLES_MARKERS = "iptables_markers"

MARKING_TYPES = [NO_MARKERS, BUCKETS_MARKERS, IPTABLES_MARKERS]

TECH_OVS = "ovs"
TECH_NONE = "none"

MIN_BAND_WIDTH = 10**6

# --------------------- TEST PARAMETERS ------------------
FIRST_TCP_PORT = 5001
FIRST_UDP_PORT = 5201
STANDALONE = "standalone"
UGUALE = "uguale"
SWITCH_TYPES = [STANDALONE, UGUALE]
IPERF_REPORT_INTERVAL = 1 
DURATION = 180 # duration of tests
MAX_TRIES = 2 # max failed tries to declare a test/configuration failed
SYNC_TIME = 10 # after this time from t0(server) users start iperf connections
RESULTS_CSV_FILENAME = "results_symm.csv"

# --------------------- VALIDITY PARAMETERS ------------------
RECORD_BEGIN = 35 # seconds to discard at the begin
RECORD_END = 3 # seconds to discard at the end
BIRTH_TIMEOUT = 20 # every user must be born within this interval
HIST_BINS = 500 # number of histogram


# ----------------- OTHER PARAMETERS ------------------

# Abbreviations to create pickle file names
# [[full_name, abbr.]]
INSTANCE_NAME_PARAMS = [
	["cookie", 			"test"	], 
	["bn_cap", 			"cap"	], 
	["free_b", 			"fb"	], 	
	["n_users", 		"u"		], 
	["range_rtts", 		"rtt"	], 
	["range_conns", 	"conn"	], 
	["duration", 		"d"		], 
	["repetition", 		"rep"	], 
	["num_bands", 		"nb"	], 
	["guard_bands", 	"gb"	], 
	["markers", 		"mr"	], 
	["tech", 			"tk"	], 
	["queuelen", 		"q"		], 
	["switch_type", 	"t"		], 
	["do_comp_rtt", 	"crtt"	], 
	["strength", 		"s"		],
	["do_symm",			"ds"	],
	["symm_width", 		"symw"	]
]

# ----------------- CSV COLUMNS ------------------

# Columns for the CSV file about test configuration (full_names)
PARAMS_COLUMNS = [
	"start_ts", "cookie", "switch_type", "bn_cap", "free_b",
	"vr_limit", "n_users", "range_rtts", "range_conns",
	"duration", "markers", "num_bands", "do_comp_rtt",
	"strength", "guard_bands", "queuelen", "tech",	
	"list_users", "fixed_conns", "fixed_rtts", "do_symm", "symm_width"]

# Columns for the CSV file about test statistics (full_names)
STATS_COLUMNS = [
	"jain_idx_mean", "jain_idx_var", 
	"thr_mean", "thr_var", 
	"good_mean", "good_var", 
	"ratio_gt_mean", "ratio_gt_var", 
	"distr_mean", "distr_var", "distr_std", "distr_mse"]


# ----------------- PDF PARAMETERS ------------------
# The parameters shown in the pdf must be a subsets of CSV names

# subset of PARAMS_COLUMNS 
PARAMS_BRIEF = [
	"switch_type", "markers", "queuelen", 
	"free_b", "num_bands", "guard_bands", "do_comp_rtt", "strength"
]
# Names of PARAMS_BRIEF to be printed
PARAMS_BRIEF_SHOW = [
	"Switch type", "Marking type", "Switch queue lenght", 
	"Unused capacity", "N. of bands", "N. of guard bands", "RTT compensation", "h"
]


# subset of PARAMS_COLUMNS (if symmetric bands are used)
PARAMS_BRIEF_SYMM = [
	"switch_type", "markers", "queuelen", 
	"num_bands", "do_symm", "symm_width"
]
# Names of PARAMS_BRIEF to be printed
PARAMS_BRIEF_SYMM_SHOW = [
	"Switch type", "Marking type", "Switch queue lenght", 
	"N. of bands", "Symm. bands", "Symm. width"
]

# subset of PARAMS_COLUMNS (if standalone switch is used)
PARAMS_BRIEF_STANDALONE = ["switch_type", "queuelen"]
# Names of PARAMS_BRIEF to be printed
PARAMS_BRIEF_STANDALONE_SHOW = ["Switch type", "Switch queue lenght"]

# subset of STAT_COLUMNS
STATS_BRIEF = [
	"jain_idx_mean", "jain_idx_var", 
	"thr_mean", "thr_var", 
	"good_mean", "good_var", 
	"ratio_gt_mean", "ratio_gt_var"
]

# Names of STATS_BRIEF to be printed
STATS_BRIEF_SHOW = [
	"Jain's Index mean", "Jain's Index var", 
	"Throughput mean", "Throughput var", 
	"Goodput mean", "Goodput var", 
	"Ratio G/T mean", "Ratio G/T var"
]


"""
Given the params and the stats of a test,
append them to the CSV file
"""
def append_to_csv(params, stats):

	rows_to_write = []
	if not os.path.isfile(RESULTS_CSV_FILENAME):
		rows_to_write.append(PARAMS_COLUMNS + STATS_COLUMNS)

	rows_to_write.append([])
	for key in PARAMS_COLUMNS:
		if key in params:
			value = str(params[key])
		else:
			value = "unknown"
		rows_to_write[-1].append(value)

	if len(stats)>0:
		rows_to_write[-1].extend([stats[c] for c in STATS_COLUMNS])
	else:
		rows_to_write[-1].extend([-1] * len(STATS_COLUMNS))

	with open(RESULTS_CSV_FILENAME, "a") as f:
		for row in rows_to_write:
			f.write(";".join(map(str, row)) + "\n")

# ------------------------ PRINTING/CONVERSIONS -------------------------#

"""
Conversion es. "45.5m"--> 45500000
Conversion es. "45m"  --> 45000000	
The rate can contain an integer.
Return an int because a bitrate is always integer
"""
def rate_to_int(rate_str):
	try:
		return int(float(rate_str))
	except ValueError:
		regex_rate = re.compile('([0-9]{1,20}[.]{0,1}[0-9]{0,20})([m,g,k]{0,1})')
		m = regex_rate.match(rate_str)
		if m is not None:
			num = float(m.groups()[0])
			mult = m.groups()[1]

			if mult=="k":
				return int(num * 10**3)
			if mult=="m":
				return int(num * 10**6)
			if mult=="g":
				return int(num * 10**9)
		print "string={}, match={}".format(rate_str, m)

"""
Conversion es. 1000-->1.0k
"""
def num_to_rate(rate_int):
	if rate_int<10**3:
		return str(rate_int)
	if rate_int<10**6:
		return str(rate_int / 10.0**3) + "k"
	if rate_int<10**9:
		return str(rate_int / 10.0**6) + "m"
	return str(rate_int / 10.0**9) + "g"

"""
Conversion es. 1000-->1k
"""
def num_to_rate_int(rate_int):
	if rate_int<10**3:
		return str(int(rate_int))
	if rate_int<10**6:
		return str(int(rate_int / 10**3)) + "k"
	if rate_int<10**9:
		return str(int(rate_int / 10**6)) + "m"
	return str(int(rate_int / 10**9)) + "g"

"""
Insert a timestamp and a return.
"""
def my_log(text, t0=0):
	print "{}: {}\r".format(time.time() - t0, text)


# ------------------------ EXECUTION OF EXTERNAL PROGRAMS -------------------------#

"""
Executes a programm (command string) and returns output lines
"""
def runPexpect(exe):
	child = pexpect.spawn(exe, timeout=None)
	for line in child:
		yield line

"""
Executes a command in background (no output!)
"""
def launch_bg(command, do_print=False):
	if do_print:
		print command
	return subprocess.Popen(command.split(), stdout=FNULL)


"""
Execute a command in the shell
"""
def cmd(command):
	subprocess.call(command, shell=True) 

"""
Execute a sudo command in the shell
"""
def sudo_cmd(command):
	subprocess.call("sudo {}".format(command), shell=True) 

"""
Open an XTERM and send an SSH command
"""
def cmd_ssh_xterm(pc_ip, command):
	cmd_str = "(xterm -hold -e \"ssh {} '{}'\") & ".format(pc_ip, command)
	cmd(cmd_str)
	print cmd_str

"""
Send an SSH command and wait for
the remote task to complete.
"""
def cmd_ssh(host, remoteCmd):
	# localCmd = "/usr/bin/ssh", host, "<<", "EOF\n{}\nEOF".format(remoteCmd)
	localCmd = "/usr/bin/ssh", host, remoteCmd
	print "*** Executing SSH command: {}".format(localCmd)
	try:
		result = subprocess.check_output(
			localCmd, stderr=subprocess.STDOUT, shell=False)
	except subprocess.CalledProcessError as e:
		result = e.output
		print "*** Error with SSH command {}: {}".format(localCmd, result)
	return result

"""
Send an SSH command and return immediately.
"""
def cmd_ssh_bg(host, remoteCmd):
	# localCmd = "/usr/bin/ssh", host, "<<", "EOF\n{}\nEOF".format(remoteCmd)
	localCmd = "/usr/bin/ssh", host, remoteCmd
	print "*** Executing SSH command in BG: {}".format(localCmd)
	subprocess.Popen(
			localCmd, stdout=FNULL, stderr=FNULL, shell=False)

""" 
Effectivelly kill all process with given name 
and optional arguments (used to launch the process)
"""
def killall(process_name, arg=None):
	if arg is not None:
		grep_str = "{}.*{}".format(process_name, arg)
	else:
		grep_str = str(process_name)
	cmd_str = "for pid in $(ps -ef | grep \"" + grep_str + "\" | awk '{print $2}'); do sudo kill -9 $pid; done"
	cmd(cmd_str)

# ------------------------ OTHER UTILITIES -------------------------#

"""
Calculate the optimale queue lenght with the Appenzeller formula:
q_opt= (RTT*C)/sqrt(n_flows)
"""
def optimal_queue_len(rtts, conns, C):
	rtt_sec = np.mean(rtts)/1000.0
	num_flows = np.sum(conns)
	length_bit = (rtt_sec*C)/float(math.sqrt(num_flows))
	length_pacc = int(length_bit/float(8*MTU)) # bit --> bytes --> packets

	"""
	Boundaries to the queue lenght
	- at least 2 packets per flow
	- maximum 10000 packets
	"""
	length_pacc = max(num_flows*2, length_pacc) 
	length_pacc = min(length_pacc, 10000)
	return length_pacc
"""
Make an interface negotiate for 100Mbps or 1Gbps
"""
def limit_interface(limit, intf):
	limit_int = rate_to_int(limit)
	print "{} ---> {}".format(limit, limit_int)
	if limit_int == (100 * 10**6):
		print "Autoneg {}@100Mbps".format(intf)
		sudo_cmd("ethtool -s {} advertise 0x008 autoneg on \
			speed 100 duplex full".format(intf))
	else:
		print "Autoneg {}@1Gbps".format(intf)
		sudo_cmd("ethtool -s {} advertise 0x020 autoneg on \
			speed 1000 duplex full".format(intf))
	time.sleep(5)

"""
Reset the network configuration for Redfox1,2,3
"""
def reset_pcs():
	for pc_address in sorted(ADDRESSES):
		str_ssh = "sudo sh delete_ovs.sh {}".format(pc_address)
		cmd_ssh_bg(pc_address, str_ssh)
	time.sleep(2)

"""
Reset the network configuration for Redfox0
"""
def reset_switch():
	cmd_ssh(SWITCH_IP, "sudo sh reset_redfox0.sh")

"""
Given a test configuration, return its filename
"""
def get_instance_name(configuration):
	instance_name = ""
	for key in INSTANCE_NAME_PARAMS:
		param = key[0]
		short_name = key[1]
		value = str(configuration[param]).replace(" ", "") 
		instance_name += ("{}{}_".format(short_name, value))
	instance_name = instance_name[:-1]
	return instance_name


"""
Set a queue length on a given interface
"""
def set_queuelen(intf, length):
	sudo_cmd("ifconfig {} txqueuelen {}".format(intf, int(length)))	


"""
Return the boolean True if val is "True" or "true" or True or 1
"""
def my_bool(val):
	if val in ["True", "true", "1", True, 1]:
		return True
	return False

"""
Cast an element (or the elements of a list, recursively)
to 3 decimal digits.
"""
def cast_value(value):
	if isinstance(value, list) or isinstance(value, tuple):
		# i'm a list
		return [cast_value(v) for v in value]
	elif isinstance(value, dict):
		return {key: cast_value(value[key]) for key in value}
	else:
		# i'm a scalar
		value = str(value)
		try:
			return int(value)
		except ValueError:
			try:
				return round(float(value), 3)
			except ValueError:
				regex_float_rate = re.compile('([0-9]{1,20}.[0-9]{1,20})([m,g,k]{0,1})')
				m = regex_float_rate.match(value)
				if m:
					return "{:.3f}{}".format(float(m.groups()[0]), m.groups()[1])
				else:
					return value
