import os, re, pexpect, subprocess, math, time
import numpy as np

FNULL = open(os.devnull, "w")

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