# plot-iperf
##Required SW
- python 2.7
- bwm-ng
```
sudo apt-get install bwm-ng
```

##Required libs
- python-matplotlib
- python-pexpect
- python-scipy
```
sudo apt-get install python-matplotlib python-pexpect python-scipy
```

#Roadmap
- [x] Initial commit
- [x] Fix plot_server (udp/tcp sum, dead declarations)
- [ ] Fix plot_client (slow? double lines? remove print)
- [ ] Write the manual

Issues
- Try not to require "sudo"
